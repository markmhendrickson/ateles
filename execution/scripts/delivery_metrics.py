#!/usr/bin/env python3
"""Compute delivery metrics for one repository from GitHub systems of record.

The operator ruled (2026-10-27) that SDLC work is measured by delivery, not by
how complete the mechanism is. This script computes the required measures for
a repo and time window:

  * idea-to-production lead time (PR lead time and merge-to-deploy time),
  * merges per day into a branch,
  * CI minutes per merged PR,
  * change-failure rate,
  * operator interventions per day (supplied by a session; not on GitHub).

Usage:
    python3 execution/scripts/delivery_metrics.py --repo owner/name \\
        --since 2026-10-13 --until 2026-10-17 [--branch main] [--json] \\
        [--interventions-file interventions.json]

The window is [since 00:00 UTC, until 00:00 UTC): ``--until 2026-10-17`` means
"through the end of 16 October". ``--until`` defaults to now.

Read-only. Stdlib only. GitHub is reached through ``gh api`` (so the active
``gh`` auth, or ``GH_TOKEN``, is used; the token is never read or printed here).

Reporting rules (these are the point of the tool):
  * Every metric carries ``n``, its sample size.
  * A metric that cannot be computed is ``status: unavailable`` with a reason,
    never 0.
  * A zero that could be an instrument fault (zero merges on a branch that
    other branches are merging into, CI minutes of 0 while runs exist) is
    flagged in ``warnings`` or turned into ``unavailable``, not reported as a
    fact.
  * Every list is paginated and the number of API calls is bounded
    (``--max-calls``); a truncated window is reported as truncated.

Definitions (also emitted in the output so the numbers carry their own meaning):
  * PR lead time: PR ``created_at`` -> ``merged_at``; and earliest commit
    author date among the PR's commits -> ``merged_at``. Median and p90.
  * CI minutes per merged PR: for each PR, the sum of
    (``updated_at`` - ``run_started_at``) over completed Actions runs with
    event in {pull_request, push} whose ``head_sha`` is one of the PR's commits
    or its merge commit. This is wall-clock per run, not billed minutes; runs
    that overlap are summed. Only the latest attempt of a re-run is visible.
  * CI failure rate: failed runs / (success + failed) runs among those same
    attributed runs; failed = failure, timed_out, startup_failure. Cancelled,
    skipped, stale, neutral and action_required runs are excluded.
  * Change-failure rate: merged PRs into the branch that are reverts (title
    starts with "Revert" or body says "reverts #N") or labelled hotfix or
    incident, divided by merged PRs into the branch in the window.
  * Deploy lead time: merge -> first successful deploy (GitHub Deployments
    status ``success``, or a successful run of a workflow whose name matches
    "deploy" on a non-pull_request event) whose SHA is the PR's merge commit or
    a descendant of it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PER_PAGE = 100
#: GitHub returns at most this many results for a filtered Actions runs query
#: (``created=`` and friends), however many pages are requested. Windows that
#: exceed it are bisected in time.
SEARCH_CAP = 1000

DEFAULT_MAX_CALLS = 1500
DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_COMPARE = 150
DEFAULT_MAX_DEPLOYMENTS = 150
DEFAULT_CI_LOOKBACK_DAYS = 30
DEPLOY_HORIZON_DAYS = 14

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
CI_EVENTS = {"pull_request", "push"}
FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
SUCCESS_CONCLUSIONS = {"success"}
FAILURE_LABELS = {"hotfix", "incident"}
REVERT_BODY_RE = re.compile(r"\breverts?\s+(?:[\w.-]+/[\w.-]+)?#\d+", re.IGNORECASE)
DEPLOY_NAME_RE = re.compile(r"deploy", re.IGNORECASE)
SUMMARY_NAME_RE = re.compile(
    r"(?<![a-z])(ci|required|summary|gate|checks?|status)(?![a-z])", re.IGNORECASE
)

DEFINITIONS = {
    "window": "[since 00:00 UTC, until 00:00 UTC); merges are attributed to the UTC date of merged_at.",
    "merges_per_day": "Merged PRs whose base is the branch, divided by the window length in days.",
    "pr_lead_time": (
        "PR created_at -> merged_at, and earliest commit author date among the PR's "
        "commits -> merged_at. Median and p90 over merged PRs."
    ),
    "ci_minutes_per_pr": (
        "Per merged PR: sum of (updated_at - run_started_at) over completed Actions runs "
        "(event pull_request or push) whose head_sha is one of the PR's commits or its "
        "merge commit. Wall-clock per run, not billed minutes. PRs with no matched run are "
        "excluded from the distribution and counted separately."
    ),
    "ci_failure_rate": (
        "failed / (success + failed) over the attributed runs; failed = failure, "
        "timed_out, startup_failure; cancelled, skipped, stale, neutral, action_required excluded."
    ),
    "change_failure_rate": (
        "Merged PRs into the branch that are reverts (title starts with 'Revert' or body "
        "says 'reverts #N') or labelled hotfix/incident, divided by merged PRs into the "
        "branch in the window."
    ),
    "deploy_lead_time": (
        "Merge -> first successful deploy (GitHub Deployments status success, or a "
        "successful non-pull_request run of a workflow named like 'deploy') whose SHA is the "
        "PR's merge commit or a descendant of it."
    ),
    "operator_interventions_per_day": (
        "Supplied by --interventions-file (not on GitHub); total in window divided by window days."
    ),
}


# --------------------------------------------------------------------------
# Errors, time helpers, statistics
# --------------------------------------------------------------------------


class ApiError(Exception):
    """A GitHub API call failed. ``status`` is the HTTP status when known."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class BudgetExceeded(Exception):
    """The --max-calls budget was spent."""


def parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_window_arg(value: str) -> datetime:
    """``YYYY-MM-DD`` (UTC midnight) or a full ISO timestamp."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    dt = parse_ts(value)
    if dt is None:
        raise argparse.ArgumentTypeError(f"not a date or ISO timestamp: {value!r}")
    return dt


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1) of a non-empty list."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "n": extra.pop("n", 0), **extra}


def distribution(values: list[float], unit: str, what: str) -> dict[str, Any]:
    """Median/p90 summary; unavailable (not 0) when there is no sample."""
    if not values:
        return unavailable(f"no samples: {what}")
    return {
        "status": "ok",
        "n": len(values),
        "unit": unit,
        "median": round(percentile(values, 0.5), 1),
        "p90": round(percentile(values, 0.9), 1),
    }


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------


def gh_transport(path: str, params: dict[str, Any]) -> Any:
    """GET ``path`` through ``gh api`` and return parsed JSON."""
    cmd = [
        "gh",
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        path.lstrip("/"),
    ]
    for key, value in params.items():
        cmd += ["-f", f"{key}={value}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(None, f"gh invocation failed: {exc}") from exc
    if result.returncode != 0:
        text = (result.stderr or "") + (result.stdout or "")
        match = re.search(r"HTTP (\d{3})", text)
        status = int(match.group(1)) if match else None
        raise ApiError(status, text.strip()[:300])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(None, f"non-JSON response from gh api {path}") from exc


class Api:
    """Counts and bounds calls over an injectable transport (tests inject a fake)."""

    def __init__(
        self,
        transport: Callable[[str, dict[str, Any]], Any],
        max_calls: int = DEFAULT_MAX_CALLS,
        per_page: int = PER_PAGE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ):
        self.transport = transport
        self.max_calls = max_calls
        self.per_page = per_page
        self.max_pages = max_pages
        self.calls = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"API call budget of {self.max_calls} spent")
        self.calls += 1
        return self.transport(path, dict(params or {}))

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        item_key: str | None = None,
        max_pages: int | None = None,
        stop: Callable[[list[Any]], bool] | None = None,
    ) -> tuple[list[Any], dict[str, Any]]:
        """Fetch pages until a short page, ``stop(page_items)`` or a bound.

        Returns (items, meta). ``meta`` has ``pages``, ``truncated`` (a bound
        cut the listing short; ``truncated_reason`` says which) and
        ``total_count`` when the endpoint supplies one.
        """
        limit = max_pages or self.max_pages
        items: list[Any] = []
        meta: dict[str, Any] = {"pages": 0, "truncated": False, "total_count": None}
        page = 1
        while True:
            if page > limit:
                meta.update(
                    truncated=True,
                    truncated_reason=f"page limit {limit} reached for {path}",
                )
                break
            try:
                data = self.get(
                    path, {**(params or {}), "per_page": self.per_page, "page": page}
                )
            except BudgetExceeded as exc:
                meta.update(truncated=True, truncated_reason=str(exc))
                break
            if item_key is not None:
                if meta["total_count"] is None and isinstance(data, dict):
                    meta["total_count"] = data.get("total_count")
                batch = data.get(item_key, []) if isinstance(data, dict) else []
            else:
                batch = data if isinstance(data, list) else []
            meta["pages"] += 1
            items.extend(batch)
            if len(batch) < self.per_page:
                break
            if stop is not None and stop(batch):
                break
            page += 1
        return items, meta


class Notes:
    """Collects truncation notices and warnings during collection."""

    def __init__(self) -> None:
        self.truncation: list[str] = []
        self.warnings: list[str] = []

    def from_meta(self, label: str, meta: dict[str, Any]) -> None:
        if meta.get("truncated"):
            self.truncation.append(
                f"{label}: {meta.get('truncated_reason', 'truncated')}"
            )


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def collect_merged_prs(
    api: Api, repo: str, branch: str, since: datetime, until: datetime, notes: Notes
) -> list[dict[str, Any]]:
    """Merged PRs with base == branch and merged_at in [since, until)."""

    def stop(batch: list[dict[str, Any]]) -> bool:
        # Sorted by updated desc: merged_at <= updated_at, so once a page ends
        # before `since` nothing deeper can have been merged in the window.
        last = parse_ts(batch[-1].get("updated_at"))
        return last is not None and last < since

    raw, meta = api.paginate(
        f"/repos/{repo}/pulls",
        {"state": "closed", "base": branch, "sort": "updated", "direction": "desc"},
        stop=stop,
    )
    notes.from_meta("pull request listing", meta)
    return _in_window_merged(raw, since, until)


def _in_window_merged(
    raw: list[dict[str, Any]], since: datetime, until: datetime
) -> list[dict[str, Any]]:
    prs: dict[int, dict[str, Any]] = {}
    for item in raw:
        merged_at = parse_ts(item.get("merged_at"))
        if merged_at is None or not (since <= merged_at < until):
            continue
        number = item.get("number")
        prs[number] = {
            "number": number,
            "title": item.get("title") or "",
            "body": item.get("body") or "",
            "labels": [
                str(lbl.get("name", "")).lower() for lbl in item.get("labels") or []
            ],
            "created_at": parse_ts(item.get("created_at")),
            "merged_at": merged_at,
            "merge_commit_sha": item.get("merge_commit_sha"),
            "head_sha": (item.get("head") or {}).get("sha"),
            "base_ref": (item.get("base") or {}).get("ref"),
            "commits": None,
        }
    return sorted(prs.values(), key=lambda p: (p["merged_at"], p["number"]))


def attach_commits(
    api: Api, repo: str, prs: list[dict[str, Any]], notes: Notes
) -> None:
    for pr in prs:
        items, meta = api.paginate(f"/repos/{repo}/pulls/{pr['number']}/commits")
        if meta.get("truncated"):
            # Do not guess from a partial list: leave commits unknown.
            notes.from_meta(f"commits of a PR (#{pr['number']})", meta)
            if not items:
                continue
        commits = []
        for c in items:
            authored = parse_ts(
                ((c.get("commit") or {}).get("author") or {}).get("date")
            )
            commits.append({"sha": c.get("sha"), "authored_at": authored})
        pr["commits"] = commits


def merged_elsewhere(
    api: Api, repo: str, since: datetime, until: datetime, notes: Notes
) -> dict[str, int]:
    """Count merged PRs in the window by base branch (for the zero-merge diagnosis)."""

    def stop(batch: list[dict[str, Any]]) -> bool:
        last = parse_ts(batch[-1].get("updated_at"))
        return last is not None and last < since

    raw, meta = api.paginate(
        f"/repos/{repo}/pulls",
        {"state": "closed", "sort": "updated", "direction": "desc"},
        max_pages=10,
        stop=stop,
    )
    notes.from_meta("cross-branch merge listing", meta)
    counts: dict[str, int] = {}
    for item in raw:
        merged_at = parse_ts(item.get("merged_at"))
        if merged_at and since <= merged_at < until:
            base = (item.get("base") or {}).get("ref") or "?"
            counts[base] = counts.get(base, 0) + 1
    return counts


def fetch_runs(
    api: Api,
    path: str,
    start: datetime,
    end: datetime,
    notes: Notes,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """All Actions runs created in [start, end], bisecting past the search cap."""
    found: dict[Any, dict[str, Any]] = {}

    def walk(a: datetime, b: datetime) -> None:
        query = {**(params or {}), "created": f"{iso(a)}..{iso(b)}"}
        items, meta = api.paginate(
            path,
            query,
            item_key="workflow_runs",
            max_pages=math.ceil(SEARCH_CAP / api.per_page) + 1,
        )
        total = meta.get("total_count")
        if meta.get("truncated"):
            notes.from_meta(f"workflow runs ({path})", meta)
        else:
            capped = (total is not None and total > len(items)) or len(
                items
            ) >= SEARCH_CAP
            if capped and (b - a) > timedelta(seconds=2):
                mid = (a + (b - a) / 2).replace(microsecond=0)
                walk(a, mid)
                walk(mid + timedelta(seconds=1), b)
                return
            if capped:
                notes.truncation.append(
                    f"workflow runs ({path}): more than {SEARCH_CAP} runs in one second"
                )
        for run in items:
            found[run.get("id")] = run

    walk(start, end)
    return list(found.values())


def collect_deploy_records(
    api: Api,
    repo: str,
    since: datetime,
    until: datetime,
    notes: Notes,
    max_deployments: int,
    environment_re: re.Pattern[str] | None,
) -> dict[str, Any]:
    """Successful deploy events (sha, success_at) from Deployments and deploy workflows."""
    horizon = until + timedelta(days=DEPLOY_HORIZON_DAYS)
    records: list[dict[str, Any]] = []
    sources: list[str] = []
    problems: list[str] = []

    # GitHub Deployments
    try:

        def stop(batch: list[dict[str, Any]]) -> bool:
            last = parse_ts(batch[-1].get("created_at"))
            return last is not None and last < since

        deployments, meta = api.paginate(f"/repos/{repo}/deployments", stop=stop)
        notes.from_meta("deployments listing", meta)
        relevant = [
            d
            for d in deployments
            if (c := parse_ts(d.get("created_at"))) is not None
            and since <= c < horizon
            and (
                environment_re is None
                or environment_re.search(str(d.get("environment", "")))
            )
        ]
        if deployments:
            sources.append("github_deployments")
        if len(relevant) > max_deployments:
            notes.truncation.append(
                f"deployments: {len(relevant)} in range, status checked for the newest {max_deployments} only"
            )
            relevant = relevant[:max_deployments]
        for d in relevant:
            statuses, smeta = api.paginate(
                f"/repos/{repo}/deployments/{d['id']}/statuses"
            )
            notes.from_meta("deployment statuses", smeta)
            good = [
                parse_ts(s.get("created_at"))
                for s in statuses
                if s.get("state") == "success" and parse_ts(s.get("created_at"))
            ]
            if good:
                records.append(
                    {
                        "sha": d.get("sha"),
                        "success_at": min(good),
                        "source": "deployment",
                        "environment": d.get("environment"),
                    }
                )
    except ApiError as exc:
        problems.append(f"deployments API: {exc.message[:120]}")

    # Deploy workflows
    try:
        workflows, wmeta = api.paginate(
            f"/repos/{repo}/actions/workflows", item_key="workflows"
        )
        notes.from_meta("workflow listing", wmeta)
        deploy_workflows = [
            w for w in workflows if DEPLOY_NAME_RE.search(str(w.get("name", "")))
        ][:5]
        if deploy_workflows:
            sources.append("deploy_workflows")
        for w in deploy_workflows:
            runs = fetch_runs(
                api,
                f"/repos/{repo}/actions/workflows/{w['id']}/runs",
                since,
                horizon,
                notes,
            )
            for run in runs:
                if (
                    run.get("event") == "pull_request"
                    or run.get("conclusion") != "success"
                ):
                    continue
                done = parse_ts(run.get("updated_at"))
                if done is None:
                    continue
                records.append(
                    {
                        "sha": run.get("head_sha"),
                        "success_at": done,
                        "source": "workflow",
                        "environment": None,
                    }
                )
    except ApiError as exc:
        problems.append(f"workflows API: {exc.message[:120]}")

    return {"records": records, "sources": sources, "problems": problems}


# --------------------------------------------------------------------------
# Metrics (pure functions over collected data)
# --------------------------------------------------------------------------


def window_days(since: datetime, until: datetime) -> list[str]:
    days = []
    cur = since.date()
    last = (until - timedelta(microseconds=1)).date()
    while cur <= last:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def metric_merges_per_day(
    prs: list[dict[str, Any]],
    since: datetime,
    until: datetime,
    other_bases: dict[str, int] | None,
) -> dict[str, Any]:
    days = window_days(since, until)
    counts = {d: 0 for d in days}
    for pr in prs:
        key = pr["merged_at"].date().isoformat()
        if key in counts:
            counts[key] += 1
    span_days = (until - since).total_seconds() / 86400.0
    result: dict[str, Any] = {
        "status": "ok",
        "n": len(prs),
        "total_merges": len(prs),
        "window_days": round(span_days, 3),
        "mean_per_day": round(len(prs) / span_days, 2) if span_days > 0 else None,
        "series": [{"date": d, "merges": counts[d]} for d in days],
    }
    if not prs:
        result["suspicious_zero"] = True
        hint = "zero merges into this branch in the window"
        if other_bases:
            top = ", ".join(
                f"{b} ({c})"
                for b, c in sorted(other_bases.items(), key=lambda kv: -kv[1])[:5]
            )
            hint += f"; but PRs were merged into other branches in the window: {top}. Check --branch"
        else:
            hint += "; no merged PRs found on other branches either, so the window may genuinely be quiet or the repo/branch wrong"
        result["note"] = hint
    return result


def metric_pr_lead_time(prs: list[dict[str, Any]]) -> dict[str, Any]:
    if not prs:
        return unavailable("no merged PRs in window")
    opened = [
        minutes_between(p["created_at"], p["merged_at"]) for p in prs if p["created_at"]
    ]
    first_commit = []
    for p in prs:
        times = [c["authored_at"] for c in (p["commits"] or []) if c.get("authored_at")]
        if times:
            first_commit.append(minutes_between(min(times), p["merged_at"]))
    return {
        "status": "ok",
        "n": len(prs),
        "created_to_merged": distribution(opened, "minutes", "no PR with a created_at"),
        "first_commit_to_merged": distribution(
            first_commit, "minutes", "commits not retrievable for any merged PR"
        ),
    }


def attribute_runs(
    prs: list[dict[str, Any]], runs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assign each run to the PR that owns its head_sha.

    A sha can appear in more than one PR (stacked PRs share commits). Ownership
    goes to the PR whose head or merge commit is that sha, else to the PR
    merged first (the one that landed it). Runs on unrelated SHAs are dropped.
    """
    owners: dict[str, list[dict[str, Any]]] = {}
    for pr in prs:
        shas = {c["sha"] for c in (pr["commits"] or []) if c.get("sha")}
        for extra in (pr.get("merge_commit_sha"), pr.get("head_sha")):
            if extra:
                shas.add(extra)
        for sha in shas:
            owners.setdefault(sha, []).append(pr)
    by_pr: dict[int, list[dict[str, Any]]] = {}
    ambiguous = 0
    matched = unmatched = 0
    for run in runs:
        if run.get("event") not in CI_EVENTS:
            continue
        sha = run.get("head_sha")
        candidates = owners.get(sha)
        if not candidates:
            unmatched += 1
            continue
        if len(candidates) > 1:
            ambiguous += 1
            direct = [
                p
                for p in candidates
                if sha in (p.get("merge_commit_sha"), p.get("head_sha"))
            ]
            pool = direct or candidates
            owner = sorted(pool, key=lambda p: (p["merged_at"], p["number"]))[0]
        else:
            owner = candidates[0]
        by_pr.setdefault(owner["number"], []).append(run)
        matched += 1
    return {
        "by_pr": by_pr,
        "matched": matched,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
    }


def run_seconds(run: dict[str, Any]) -> float | None:
    if run.get("status") != "completed":
        return None
    start = parse_ts(run.get("run_started_at"))
    end = parse_ts(run.get("updated_at"))
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def identify_summary_workflow(
    by_pr: dict[int, list[dict[str, Any]]], required_contexts: list[str]
) -> tuple[str | None, str]:
    coverage: dict[str, int] = {}
    prs_with_pr_runs = 0
    for runs in by_pr.values():
        names = {
            r.get("name")
            for r in runs
            if r.get("event") == "pull_request" and r.get("name")
        }
        if names:
            prs_with_pr_runs += 1
        for n in names:
            coverage[n] = coverage.get(n, 0) + 1
    if not coverage:
        return None, "no pull_request runs attributed to merged PRs"
    folded = {c.casefold() for c in required_contexts}
    required = [n for n in coverage if n.casefold() in folded]
    if required:
        pick = sorted(required, key=lambda n: (-coverage[n], n))[0]
        return pick, "required_status_check"
    guesses = [
        n
        for n in coverage
        if SUMMARY_NAME_RE.search(n) and coverage[n] * 2 >= prs_with_pr_runs
    ]
    if guesses:
        pick = sorted(guesses, key=lambda n: (-coverage[n], n))[0]
        return (
            pick,
            "name heuristic (ci/required/summary/gate/checks/status, present on at least half of PRs)",
        )
    return None, (
        "no workflow name matches a required status check or the ci/required/summary/gate/checks/status "
        "heuristic on at least half of PRs"
    )


def metric_ci(
    prs: list[dict[str, Any]],
    runs_fetched: int,
    runs: list[dict[str, Any]],
    required_contexts: list[str],
    notes: Notes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (ci_minutes_per_pr, ci_failure_rate)."""
    if not prs:
        reason = "no merged PRs in window"
        return unavailable(reason), unavailable(reason)
    if any(p["commits"] is None for p in prs):
        notes.warnings.append(
            "commits unavailable for some PRs (budget or API error); their CI runs may be unattributed"
        )
    if runs_fetched == 0:
        reason = "no workflow runs returned for the period (Actions disabled, retention expired, or not accessible)"
        return unavailable(reason), unavailable(reason)

    attribution = attribute_runs(prs, runs)
    by_pr = attribution["by_pr"]
    if attribution["matched"] == 0:
        reason = (
            f"{runs_fetched} workflow runs exist in the period but none matched any merged PR commit or "
            "merge commit (matching problem, or merged PRs were never built)"
        )
        return unavailable(reason, runs_fetched=runs_fetched), unavailable(reason)

    per_pr_minutes: list[float] = []
    zero_total_prs = 0
    for pr in prs:
        durations = [
            d for r in by_pr.get(pr["number"], []) if (d := run_seconds(r)) is not None
        ]
        if not durations:
            continue
        total = sum(durations)
        if total <= 0:
            zero_total_prs += 1
        per_pr_minutes.append(total / 60.0)
    prs_without_runs = len(prs) - len(per_pr_minutes)

    base: dict[str, Any] = {
        "runs_fetched": runs_fetched,
        "runs_attributed": attribution["matched"],
        "runs_on_unrelated_shas": attribution["unmatched"],
        "runs_with_ambiguous_sha_owner": attribution["ambiguous"],
        "prs_without_matched_runs": prs_without_runs,
    }
    if not per_pr_minutes:
        ci = unavailable(
            "runs matched PRs but none was a completed run with usable timestamps",
            **base,
        )
    elif max(per_pr_minutes) <= 0:
        ci = unavailable(
            "every matched run has zero duration (run_started_at == updated_at): timestamp problem, not a fact",
            **base,
        )
    else:
        ci = {**distribution(per_pr_minutes, "minutes", ""), **base}
        if zero_total_prs:
            notes.warnings.append(
                f"{zero_total_prs} PR(s) had matched runs with zero total duration; check timestamps"
            )

    # required / summary workflow wall-clock
    workflow, how = identify_summary_workflow(by_pr, required_contexts)
    if workflow is None:
        ci["required_run_wall_clock"] = unavailable(how)
    else:
        wall: list[float] = []
        for pr in prs:
            matching = [
                r
                for r in by_pr.get(pr["number"], [])
                if r.get("name") == workflow
                and r.get("event") == "pull_request"
                and run_seconds(r) is not None
            ]
            if matching:
                latest = max(
                    matching,
                    key=lambda r: (
                        parse_ts(r.get("updated_at"))
                        or datetime.min.replace(tzinfo=timezone.utc)
                    ),
                )
                wall.append((run_seconds(latest) or 0.0) / 60.0)
        dist = distribution(
            wall, "minutes", "no PR with a completed run of the summary workflow"
        )
        ci["required_run_wall_clock"] = {
            **dist,
            "workflow": workflow,
            "identified_by": how,
        }

    # per-workflow wall-clock (latest run of each workflow per PR), so the slow
    # workflow is visible even when no single summary run can be identified
    per_workflow: dict[str, list[float]] = {}
    for pr in prs:
        latest_by_name: dict[str, dict[str, Any]] = {}
        for r in by_pr.get(pr["number"], []):
            if run_seconds(r) is None or not r.get("name"):
                continue
            prev = latest_by_name.get(r["name"])
            if prev is None or (parse_ts(r.get("updated_at")) or _EPOCH) > (
                parse_ts(prev.get("updated_at")) or _EPOCH
            ):
                latest_by_name[r["name"]] = r
        for name, r in latest_by_name.items():
            per_workflow.setdefault(name, []).append((run_seconds(r) or 0.0) / 60.0)
    ci["by_workflow"] = [
        {"workflow": name, **distribution(vals, "minutes", "")}
        for name, vals in sorted(
            per_workflow.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
    ]

    # failure rate over attributed runs
    all_attributed = [r for runs_ in by_pr.values() for r in runs_]
    ok = sum(
        1
        for r in all_attributed
        if r.get("status") == "completed" and r.get("conclusion") in SUCCESS_CONCLUSIONS
    )
    failed = sum(
        1
        for r in all_attributed
        if r.get("status") == "completed" and r.get("conclusion") in FAILED_CONCLUSIONS
    )
    denom = ok + failed
    if denom == 0:
        failure = unavailable(
            "no attributed run finished with a success or failure conclusion"
        )
    else:
        failure = {
            "status": "ok",
            "n": denom,
            "failed": failed,
            "rate": round(failed / denom, 4),
            "excluded_runs": len(all_attributed) - denom,
        }
    return ci, failure


def failure_reasons(pr: dict[str, Any]) -> list[str]:
    reasons = []
    if pr["title"].lstrip().lower().startswith("revert"):
        reasons.append("revert_title")
    if REVERT_BODY_RE.search(pr["body"]):
        reasons.append("revert_body")
    if FAILURE_LABELS & set(pr["labels"]):
        reasons.append("label")
    return reasons


def metric_change_failure(prs: list[dict[str, Any]]) -> dict[str, Any]:
    if not prs:
        return unavailable("no merged PRs in window")
    flagged = [(p, failure_reasons(p)) for p in prs]
    failures = [(p, r) for p, r in flagged if r]
    return {
        "status": "ok",
        "n": len(prs),
        "failures": len(failures),
        "rate": round(len(failures) / len(prs), 4),
        "failing_prs": [p["number"] for p, _ in failures],
        "definition": DEFINITIONS["change_failure_rate"],
    }


def metric_interventions(
    entries: list[dict[str, Any]] | None, since: datetime, until: datetime
) -> dict[str, Any]:
    if entries is None:
        return unavailable(
            "no --interventions-file supplied; interventions are not recorded on GitHub"
        )
    days = window_days(since, until)
    per_day = {d: 0 for d in days}
    outside = 0
    for e in entries:
        if e["date"] in per_day:
            per_day[e["date"]] += e["count"]
        else:
            outside += 1
    total = sum(per_day.values())
    span_days = (until - since).total_seconds() / 86400.0
    result: dict[str, Any] = {
        "status": "ok",
        "n": total,
        "total": total,
        "mean_per_day": round(total / span_days, 2) if span_days > 0 else None,
        "series": [{"date": d, "interventions": per_day[d]} for d in days],
        "entries_outside_window": outside,
    }
    if total == 0:
        result["suspicious_zero"] = True
        result["note"] = (
            "file supplied but holds no interventions in the window; confirm it covers this period"
        )
    return result


def load_interventions(path: str) -> list[dict[str, Any]]:
    """Normalise a file of ``{date, count}`` or ``{at, note}`` entries."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read interventions file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"interventions file is not valid JSON: {exc}") from exc
    return normalise_interventions(raw)


def normalise_interventions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("interventions file must be a JSON list")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"interventions[{i}] is not an object")
        if "count" in entry or "date" in entry:
            count = entry.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"interventions[{i}]: count must be a non-negative integer"
                )
            date_raw = entry.get("date")
            try:
                day = datetime.fromisoformat(str(date_raw)).date().isoformat()
            except ValueError as exc:
                raise ValueError(f"interventions[{i}]: bad date {date_raw!r}") from exc
            out.append({"date": day, "count": count})
        elif "at" in entry:
            at = parse_ts(entry.get("at"))
            if at is None:
                raise ValueError(
                    f"interventions[{i}]: bad timestamp {entry.get('at')!r}"
                )
            out.append({"date": at.date().isoformat(), "count": 1})
        else:
            raise ValueError(
                f"interventions[{i}]: needs {{date, count}} or {{at, note}}"
            )
    return out


def metric_deploys(
    prs: list[dict[str, Any]],
    deploy: dict[str, Any],
    compare: Callable[[str, str], str | None],
    branch: str,
    since: datetime,
    until: datetime,
    max_compare: int,
    notes: Notes,
) -> dict[str, Any]:
    if not prs:
        return unavailable("no merged PRs in window")
    records = sorted(deploy["records"], key=lambda r: r["success_at"])
    if not deploy["sources"]:
        reason = "no GitHub Deployments and no workflow named like 'deploy' found"
        if deploy["problems"]:
            reason += " (" + "; ".join(deploy["problems"]) + ")"
        return unavailable(reason)
    if not records:
        return unavailable(
            "deploy sources exist ("
            + ", ".join(deploy["sources"])
            + ") but no successful deploy was found in the period"
        )

    compare_calls = 0
    cache: dict[tuple[str, str], str | None] = {}

    def cmp(base: str, head: str) -> str | None:
        nonlocal compare_calls
        key = (base, head)
        if key in cache:
            return cache[key]
        if compare_calls >= max_compare:
            raise BudgetExceeded("compare budget spent")
        compare_calls += 1
        cache[key] = compare(base, head)
        return cache[key]

    deltas: list[float] = []
    unmatched = 0
    budget_hit = False
    for pr in prs:
        merge_sha = pr.get("merge_commit_sha")
        if not merge_sha:
            unmatched += 1
            continue
        after = [r for r in records if r["success_at"] >= pr["merged_at"] and r["sha"]]
        exact = [r for r in after if r["sha"] == merge_sha]
        hit = exact[0] if exact else None
        if hit is None:
            for r in [r for r in after if r["sha"] != merge_sha][:3]:
                try:
                    status = cmp(merge_sha, r["sha"])
                except BudgetExceeded:
                    budget_hit = True
                    break
                if status in ("ahead", "identical"):
                    hit = r
                    break
        if hit is None:
            unmatched += 1
        else:
            deltas.append(minutes_between(pr["merged_at"], hit["success_at"]))
    if budget_hit:
        notes.truncation.append(
            f"deploy lead time: compare budget of {max_compare} spent; later PRs unmatched"
        )

    lead = distribution(
        deltas,
        "minutes",
        "no merged PR was followed by a successful deploy of its commit in the data",
    )
    lead["merged_prs"] = len(prs)
    lead["prs_without_matching_deploy"] = unmatched

    # share of deploys whose SHA is on the branch
    window_records = [
        r
        for r in records
        if r["sha"]
        and since <= r["success_at"] < until + timedelta(days=DEPLOY_HORIZON_DAYS)
    ]
    shas = sorted({r["sha"] for r in window_records})
    on_branch = off_branch = unresolved = 0
    checked = 0
    for sha in shas:
        try:
            status = cmp(branch, sha)
        except BudgetExceeded:
            notes.truncation.append(
                f"deploy-on-branch share: compare budget of {max_compare} spent after {checked} SHAs"
            )
            break
        checked += 1
        if status in ("behind", "identical"):
            on_branch += 1
        elif status in ("ahead", "diverged"):
            off_branch += 1
        else:
            unresolved += 1
    if checked - unresolved == 0:
        share: dict[str, Any] = unavailable(
            "no deploy SHA could be compared with the branch",
            n=0,
            deploy_shas=len(shas),
        )
    else:
        resolved = on_branch + off_branch
        share = {
            "status": "ok",
            "n": resolved,
            "on_branch": on_branch,
            "off_branch": off_branch,
            "unresolvable": unresolved,
            "share_on_branch": round(on_branch / resolved, 4),
        }
    return {
        "status": lead["status"],
        "sources": deploy["sources"],
        "problems": deploy["problems"],
        "merge_to_deploy": lead,
        "deploy_sha_on_branch": share,
        "n": lead["n"],
        **({"reason": lead["reason"]} if lead["status"] != "ok" else {}),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def compute(
    api: Api,
    repo: str,
    branch: str | None,
    since: datetime,
    until: datetime,
    interventions: list[dict[str, Any]] | None = None,
    max_compare: int = DEFAULT_MAX_COMPARE,
    max_deployments: int = DEFAULT_MAX_DEPLOYMENTS,
    ci_lookback_days: int = DEFAULT_CI_LOOKBACK_DAYS,
    deploy_environment: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    notes = Notes()
    if until <= since:
        raise ValueError("--until must be after --since")

    if branch is None:
        repo_info = api.get(f"/repos/{repo}")
        branch = repo_info.get("default_branch")
        if not branch:
            raise ApiError(None, f"cannot determine default branch of {repo}")

    prs = collect_merged_prs(api, repo, branch, since, until, notes)
    attach_commits(api, repo, prs, notes)

    other_bases = merged_elsewhere(api, repo, since, until, notes) if not prs else None
    if other_bases is not None:
        other_bases.pop(branch, None)

    # CI runs: from shortly before the earliest PR was opened, to just after the last merge
    runs: list[dict[str, Any]] = []
    runs_fetched = 0
    required_contexts: list[str] = []
    if prs:
        earliest = min(
            (p["created_at"] for p in prs if p["created_at"]), default=since
        ) - timedelta(days=1)
        floor = since - timedelta(days=ci_lookback_days)
        if earliest < floor:
            notes.truncation.append(
                f"CI runs: lookback capped at {ci_lookback_days} days before --since; "
                "runs for older PR commits are not seen"
            )
            earliest = floor
        latest = min(
            max(p["merged_at"] for p in prs) + timedelta(days=1),
            until + timedelta(days=1),
        )
        runs = fetch_runs(api, f"/repos/{repo}/actions/runs", earliest, latest, notes)
        runs_fetched = len(runs)
        try:
            prot = api.get(
                f"/repos/{repo}/branches/{branch}/protection/required_status_checks"
            )
            required_contexts = list(prot.get("contexts") or []) + [
                c.get("context") for c in prot.get("checks") or [] if c.get("context")
            ]
        except (ApiError, BudgetExceeded):
            required_contexts = []

    ci, ci_failure = metric_ci(prs, runs_fetched, runs, required_contexts, notes)

    env_re = (
        re.compile(deploy_environment, re.IGNORECASE) if deploy_environment else None
    )
    if prs:
        deploy = collect_deploy_records(
            api, repo, since, until, notes, max_deployments, env_re
        )

        def compare(base: str, head: str) -> str | None:
            try:
                data = api.get(
                    f"/repos/{repo}/compare/{base}...{head}", {"per_page": 1}
                )
            except ApiError:
                return None
            return data.get("status") if isinstance(data, dict) else None

        deploys = metric_deploys(
            prs, deploy, compare, branch, since, until, max_compare, notes
        )
    else:
        deploys = unavailable("no merged PRs in window")

    merges = metric_merges_per_day(prs, since, until, other_bases)
    if merges.get("suspicious_zero"):
        notes.warnings.append("zero merges into " + branch + ": " + merges["note"])

    metrics = {
        "merges_per_day": merges,
        "pr_lead_time": metric_pr_lead_time(prs),
        "ci_minutes_per_pr": ci,
        "ci_failure_rate": ci_failure,
        "change_failure_rate": metric_change_failure(prs),
        "deploy_lead_time": deploys,
        "operator_interventions_per_day": metric_interventions(
            interventions, since, until
        ),
    }
    return {
        "repo": repo,
        "branch": branch,
        "window": {"since": iso(since), "until": iso(until)},
        "generated_at": iso(now or datetime.now(timezone.utc)),
        "api_calls": api.calls,
        "max_calls": api.max_calls,
        "truncated": bool(notes.truncation),
        "truncation_notes": notes.truncation,
        "warnings": notes.warnings,
        "definitions": DEFINITIONS,
        "metrics": metrics,
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def fmt_minutes(value: float | None) -> str:
    if value is None:
        return "n/a"
    m = float(value)
    if m < 90:
        return f"{m:.0f}m"
    if m < 60 * 36:
        return f"{m / 60:.1f}h"
    return f"{m / 1440:.1f}d"


def _dist_line(label: str, d: dict[str, Any]) -> str:
    if d.get("status") != "ok":
        return f"  {label}: unavailable ({d.get('reason')})"
    return f"  {label}: median {fmt_minutes(d['median'])}, p90 {fmt_minutes(d['p90'])} (n={d['n']})"


def render_summary(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        f"Delivery metrics for {report['repo']} into {report['branch']}",
        f"Window {report['window']['since']} -> {report['window']['until']}"
        f" ({report['api_calls']} API calls)",
    ]
    if report["truncated"]:
        lines.append("WINDOW TRUNCATED: " + "; ".join(report["truncation_notes"]))
    for w in report["warnings"]:
        lines.append(f"WARNING: {w}")
    lines.append("")

    mp = m["merges_per_day"]
    lines.append(
        f"Merges per day: {mp['mean_per_day']} mean ({mp['total_merges']} merges over {mp['window_days']} days, n={mp['n']})"
        + ("  [SUSPICIOUS ZERO]" if mp.get("suspicious_zero") else "")
    )
    lines.append(
        "  per day: "
        + ", ".join(f"{s['date'][5:]}:{s['merges']}" for s in mp["series"])
    )

    lt = m["pr_lead_time"]
    lines.append("PR lead time:")
    if lt["status"] != "ok":
        lines.append(f"  unavailable ({lt['reason']})")
    else:
        lines.append(_dist_line("created -> merged", lt["created_to_merged"]))
        lines.append(_dist_line("first commit -> merged", lt["first_commit_to_merged"]))

    ci = m["ci_minutes_per_pr"]
    lines.append("CI minutes per merged PR (wall-clock sum of attributed runs):")
    if ci["status"] != "ok":
        lines.append(f"  unavailable ({ci['reason']})")
    else:
        lines.append(_dist_line("per PR", ci))
        lines.append(
            f"  runs: {ci['runs_attributed']} attributed of {ci['runs_fetched']} fetched;"
            f" {ci['prs_without_matched_runs']} PR(s) with no matched run"
        )
        lines.append(
            _dist_line("required/summary run wall-clock", ci["required_run_wall_clock"])
        )
        for w in ci.get("by_workflow", []):
            lines.append(_dist_line(f"workflow '{w['workflow']}' wall-clock", w))
    cf = m["ci_failure_rate"]
    if cf["status"] == "ok":
        lines.append(
            f"CI failure rate: {cf['rate']:.1%} ({cf['failed']} failed of {cf['n']} finished runs)"
        )
    else:
        lines.append(f"CI failure rate: unavailable ({cf['reason']})")

    chf = m["change_failure_rate"]
    if chf["status"] == "ok":
        lines.append(
            f"Change-failure rate: {chf['rate']:.1%} ({chf['failures']} of {chf['n']} merged PRs are reverts or hotfix/incident)"
        )
    else:
        lines.append(f"Change-failure rate: unavailable ({chf['reason']})")

    dp = m["deploy_lead_time"]
    lines.append("Deploy lead time:")
    if dp["status"] != "ok":
        lines.append(f"  unavailable ({dp.get('reason')})")
    else:
        lines.append(_dist_line("merge -> successful deploy", dp["merge_to_deploy"]))
        lines.append(
            f"  merged PRs with no matching deploy: {dp['merge_to_deploy']['prs_without_matching_deploy']}"
        )
    if dp.get("deploy_sha_on_branch"):
        sb = dp["deploy_sha_on_branch"]
        if sb["status"] == "ok":
            lines.append(
                f"  deploy SHAs on {report['branch']}: {sb['share_on_branch']:.1%} (n={sb['n']})"
            )
        else:
            lines.append(f"  deploy SHAs on branch: unavailable ({sb['reason']})")

    oi = m["operator_interventions_per_day"]
    if oi["status"] == "ok":
        lines.append(
            f"Operator interventions per day: {oi['mean_per_day']} mean ({oi['total']} total)"
            + ("  [SUSPICIOUS ZERO]" if oi.get("suspicious_zero") else "")
        )
    else:
        lines.append(f"Operator interventions per day: unavailable ({oi['reason']})")
    lines.append("")
    lines.append("Change-failure definition: " + DEFINITIONS["change_failure_rate"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument(
        "--since",
        required=True,
        type=parse_window_arg,
        help="YYYY-MM-DD (UTC) start, inclusive",
    )
    p.add_argument(
        "--until",
        type=parse_window_arg,
        default=None,
        help="YYYY-MM-DD (UTC) end, exclusive; default now",
    )
    p.add_argument(
        "--branch",
        default=None,
        help="merge target branch; default: the repo's default branch",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--interventions-file",
        default=None,
        help="JSON list of {date,count} or {at,note}",
    )
    p.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help="bound on GitHub API calls",
    )
    p.add_argument(
        "--max-compare",
        type=int,
        default=DEFAULT_MAX_COMPARE,
        help="bound on compare API calls",
    )
    p.add_argument("--max-deployments", type=int, default=DEFAULT_MAX_DEPLOYMENTS)
    p.add_argument("--ci-lookback-days", type=int, default=DEFAULT_CI_LOOKBACK_DAYS)
    p.add_argument(
        "--deploy-environment",
        default=None,
        help="regex restricting GitHub Deployments by environment",
    )
    return p


def main(
    argv: list[str] | None = None,
    transport: Callable[[str, dict[str, Any]], Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        print("error: --repo must be owner/name", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    until = args.until or now.replace(microsecond=0)
    interventions = None
    if args.interventions_file:
        try:
            interventions = load_interventions(args.interventions_file)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    api = Api(transport or gh_transport, max_calls=args.max_calls)
    try:
        report = compute(
            api,
            args.repo,
            args.branch,
            args.since,
            until,
            interventions=interventions,
            max_compare=args.max_compare,
            max_deployments=args.max_deployments,
            ci_lookback_days=args.ci_lookback_days,
            deploy_environment=args.deploy_environment,
            now=now,
        )
    except (ApiError, BudgetExceeded, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(report, indent=2, sort_keys=False)
        if args.json
        else render_summary(report)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
