#!/usr/bin/env python3
"""Repo baseline: public-endpoint pull for npm downloads and GitHub activity (build-plan step 2).

Metrics produced, one ``metric_reading`` per metric, artifact and week:

  D1 npm_downloads_weekly      npm downloads API, Monday-Sunday UTC weeks
  D2 github_stars              weekly net new stars from ``starred_at``; cumulative in detail
  D4 external_issues_opened    issues (not PRs) opened per week by outside authors; totals in detail
  D5 external_contributors     distinct non-swarm authors of PRs merged in the trailing 28 days

Only public endpoints are used. A GitHub token is optional: if GITHUB_TOKEN (or
GH_TOKEN) is in the process it raises the rate limit from 60 to 5000 requests
an hour; it is sent only to api.github.com and never printed. To run with the
swarm App's installation token:

    python3 lib/github_app_token.py exec --repo <owner>/<repo> -- \\
        python3 execution/scripts/metrics/repo_baseline.py compute --npm-package <package> --repo <owner>/<repo>

    python3 execution/scripts/metrics/repo_baseline.py compute ...   # print readings + baseline summary
    python3 execution/scripts/metrics/repo_baseline.py store ...     # compute, then store + read back

``store`` also needs NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN in the process.

"External" means: not the repository owner, not a bot account, and not one of
the swarm's agent logins (``--swarm-login`` or METRICS_SWARM_LOGINS, comma
separated). The exclusion list actually applied is written into each reading's
``detail`` so a baseline can be recomputed. Nothing here schedules itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.scripts.metrics import neotoma_io, weekly  # noqa: E402

COLLECTOR = "repo_baseline.py"
DEFAULT_WEEKS = 26
NPM_LAG_DAYS = 1  # the registry's most recent day is incomplete; weeks must end a day before now
USER_AGENT = "ateles-metrics/1.0"
GITHUB_API = "https://api.github.com"
NPM_API = "https://api.npmjs.org"
# fetch(url, accept) -> (parsed JSON, lower-cased response headers)
Fetcher = Callable[[str, str], "tuple[Any, dict[str, str]]"]


# --- HTTP ----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would forward the Authorization header to the new host."""

    def redirect_request(self, *args: Any, **kwargs: Any):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _github_token() -> str:
    """The optional token from the process, stripped; an empty value means no token."""
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _redact(text: str, token: str) -> str:
    return text.replace(token, "[REDACTED]") if token else text


def http_get(url: str, accept: str = "application/json", *, timeout: float = 30.0,
             retries: int = 3) -> tuple[Any, dict[str, str]]:
    """GET JSON; the GitHub token (if any) goes only to api.github.com. Never printed.

    Same protections as ``lib.capabilities.neotoma_http.request_json``: redirects
    are never followed (so the token cannot ride a redirect to another host), the
    token is stripped before use, and no exception raised from here carries it.
    """
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    token = _github_token()
    if token and url.startswith(GITHUB_API + "/"):
        headers["Authorization"] = f"Bearer {token}"
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
                return json.load(resp), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            body = _redact((exc.read() or b"")[:300].decode("utf-8", "replace"), token)
            if exc.status in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            if exc.status == 403 and "rate limit" in body.lower():
                raise RuntimeError("GitHub rate limit reached; run with an App installation token "
                                   "(lib/github_app_token.py exec) or wait for the reset") from None
            raise RuntimeError(f"HTTP {exc.status} for {url.split('?')[0]}: {body}") from None
        except OSError as exc:
            if attempt == retries:
                raise OSError(_redact(str(exc), token)) from None
            time.sleep(delay)
            delay *= 2
        except json.JSONDecodeError:
            raise RuntimeError(f"non-JSON response from {url.split('?')[0]}") from None
        except ValueError:
            # http.client rejects a header value with CR/LF and quotes the whole value in its message.
            raise RuntimeError(f"request to {url.split('?')[0]} failed: invalid request header "
                               "(check GITHUB_TOKEN / GH_TOKEN for stray control characters)") from None
    raise RuntimeError("unreachable")


class PaginationError(RuntimeError):
    """A paged GitHub read could not be completed; a partial list must not be stored as complete."""


def paginate(url: str, *, accept: str = "application/json", fetch: Fetcher | None = None,
             max_pages: int = 400) -> Iterable[Any]:
    """Yield items across Link rel=next pages (same host only).

    Raises :class:`PaginationError` when a next link points off-host or the page
    cap is reached, rather than ending the list quietly.
    """
    getter = fetch or http_get
    next_url: str | None = url
    pages = 0
    while next_url:
        if pages >= max_pages:
            raise PaginationError(f"more than {max_pages} pages from {url.split('?')[0]}")
        data, headers = getter(next_url, accept)
        yield from data
        pages += 1
        match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("link", ""))
        if not match:
            return
        next_url = match.group(1)
        if not next_url.startswith(GITHUB_API + "/"):
            raise PaginationError(f"next link leaves {GITHUB_API} (from {url.split('?')[0]}); list incomplete")


# --- parsers (pure) ---------------------------------------------------------------

def parse_npm_range(payload: dict[str, Any]) -> dict[str, int]:
    """npm range payload to {YYYY-MM-DD: downloads}; a payload with no ``downloads`` key is an error, not zero."""
    if "downloads" not in payload:
        raise ValueError("npm response has no 'downloads' key; refusing to read it as zero downloads")
    return {row["day"]: int(row["downloads"]) for row in payload.get("downloads", [])}


def weekly_npm(daily: dict[str, int], windows: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    out = []
    for start, end in windows:
        days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        present = [d for d in days if d in daily]
        out.append({"start": start, "end": end, "value": sum(daily[d] for d in present),
                    "coverage": len(present) / 7})
    return out


def parse_star_times(items: Iterable[dict[str, Any]]) -> list[datetime]:
    return sorted(weekly.parse_ts(it["starred_at"]) for it in items if it.get("starred_at"))


def weekly_stars(times: list[datetime], windows: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    out = []
    for start, end in windows:
        net = sum(1 for t in times if start <= t < end)
        cumulative = sum(1 for t in times if t < end)
        out.append({"start": start, "end": end, "value": net, "cumulative": cumulative})
    return out


def is_external(user: dict[str, Any] | None, owner: str, swarm_logins: set[str]) -> bool:
    if not user:
        return False
    login = (user.get("login") or "").lower()
    if not login or login == owner.lower() or login in swarm_logins:
        return False
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        return False
    return True


def parse_issues(items: Iterable[dict[str, Any]], owner: str, swarm_logins: set[str]) -> list[dict[str, Any]]:
    """Issues only (the issues endpoint also returns PRs)."""
    out = []
    for it in items:
        if "pull_request" in it:
            continue
        out.append({
            "created": weekly.parse_ts(it["created_at"]),
            "closed": weekly.parse_ts(it["closed_at"]) if it.get("closed_at") else None,
            "external": is_external(it.get("user"), owner, swarm_logins),
        })
    return out


def weekly_issues(issues: list[dict[str, Any]], windows: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    out = []
    for start, end in windows:
        opened = [i for i in issues if start <= i["created"] < end]
        closed = [i for i in issues if i["closed"] and start <= i["closed"] < end]
        out.append({"start": start, "end": end,
                    "value": sum(1 for i in opened if i["external"]),
                    "opened_total": len(opened), "closed_total": len(closed)})
    return out


def parse_merged_prs(items: Iterable[dict[str, Any]], owner: str, swarm_logins: set[str]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        if not it.get("merged_at"):
            continue
        out.append({"merged": weekly.parse_ts(it["merged_at"]),
                    "author": (it.get("user") or {}).get("login", "").lower(),
                    "external": is_external(it.get("user"), owner, swarm_logins)})
    return out


def weekly_contributors(prs: list[dict[str, Any]], windows: list[tuple[datetime, datetime]],
                        span_days: int = 28) -> list[dict[str, Any]]:
    out = []
    for start, end in windows:
        lo = end - timedelta(days=span_days)
        in_span = [p for p in prs if lo <= p["merged"] < end]
        external = {p["author"] for p in in_span if p["external"]}
        out.append({"start": start, "end": end, "value": len(external),
                    "merged_prs": len(in_span), "span_days": span_days})
    return out


# --- readings -----------------------------------------------------------------------

def build_readings(*, package: str, repo_data: dict[str, dict[str, Any]], npm_rows: list[dict[str, Any]],
                   swarm_logins: set[str], now: datetime) -> list[dict[str, Any]]:
    readings: list[dict[str, Any]] = []
    npm_artifact = f"npm:{package}"
    last_npm = npm_rows[-1]["value"] if npm_rows else 0
    for row in npm_rows:
        readings.append(weekly.make_reading(
            metric_id="D1_npm_downloads_weekly", artifact=npm_artifact, start=row["start"], end=row["end"],
            value=row["value"], unit="downloads", collector=COLLECTOR, sourced_at=now,
            coverage_ratio=row["coverage"],
            coverage_note="" if row["coverage"] >= 1 else "registry returned fewer than 7 days",
            probe_ok=last_npm > 0))
    for repo, data in repo_data.items():
        artifact = f"github:{repo}"
        excluded = sorted(swarm_logins | {repo.split("/")[0].lower()})
        cumulative_now = data["stars"][-1]["cumulative"] if data["stars"] else 0
        for row in data["stars"]:
            readings.append(weekly.make_reading(
                metric_id="D2_github_stars", artifact=artifact, start=row["start"], end=row["end"],
                value=row["value"], unit="stars", collector=COLLECTOR, sourced_at=now,
                probe_ok=cumulative_now > 0, detail={"cumulative_at_window_end": row["cumulative"]}))
        for row in data["issues"]:
            readings.append(weekly.make_reading(
                metric_id="D4_external_issues_opened", artifact=artifact, start=row["start"], end=row["end"],
                value=row["value"], unit="issues", collector=COLLECTOR, sourced_at=now,
                detail={"opened_total": row["opened_total"], "closed_total": row["closed_total"],
                        "excluded_logins": excluded, "excludes_bots": True}))
        for row in data["contributors"]:
            readings.append(weekly.make_reading(
                metric_id="D5_external_contributors", artifact=artifact, start=row["start"], end=row["end"],
                value=row["value"], unit="contributors", collector=COLLECTOR, sourced_at=now,
                detail={"span_days": row["span_days"], "merged_prs_in_span": row["merged_prs"],
                        "excluded_logins": excluded, "excludes_bots": True}))
    return readings


# --- collection ------------------------------------------------------------------------

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PACKAGE_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")


def validate_repo(repo: str) -> None:
    """Arguments are interpolated into URLs; refuse anything that is not a plain owner/name slug."""
    if not REPO_RE.match(repo):
        raise ValueError(f"not an owner/name repository: {repo!r}")


def validate_targets(package: str, repos: list[str]) -> None:
    if not PACKAGE_RE.match(package):
        raise ValueError(f"not an npm package name: {package!r}")
    for repo in repos:
        validate_repo(repo)


def collect(*, package: str, repos: list[str], weeks: int, swarm_logins: set[str], now: datetime,
            fetch: Fetcher | None = None) -> list[dict[str, Any]]:
    validate_targets(package, repos)
    getter = fetch or http_get
    windows = weekly.weeks_back(now, weeks, lag_days=NPM_LAG_DAYS)
    start, end = windows[0][0], windows[-1][1]
    payload, _ = getter(f"{NPM_API}/downloads/range/{start:%Y-%m-%d}:{end:%Y-%m-%d}/{urllib.parse.quote(package, safe='@')}",
                        "application/json")
    npm_rows = weekly_npm(parse_npm_range(payload), windows)

    repo_data: dict[str, dict[str, Any]] = {}
    for repo in repos:
        owner = repo.split("/")[0]
        star_items = paginate(f"{GITHUB_API}/repos/{repo}/stargazers?per_page=100",
                              accept="application/vnd.github.star+json", fetch=getter)
        star_times = parse_star_times(star_items)
        star_windows = weekly.weeks_between(star_times[0], now, lag_days=NPM_LAG_DAYS) if star_times else []
        cutoff = windows[0][0] - timedelta(days=28)
        issue_items = paginate(f"{GITHUB_API}/repos/{repo}/issues?state=all&since={cutoff:%Y-%m-%dT%H:%M:%SZ}&per_page=100",
                               fetch=getter)
        issues = parse_issues(issue_items, owner, swarm_logins)
        pr_items = _recent_closed_prs(repo, cutoff, getter)
        prs = parse_merged_prs(pr_items, owner, swarm_logins)
        repo_data[repo] = {
            "stars": weekly_stars(star_times, star_windows),
            "issues": weekly_issues(issues, windows),
            "contributors": weekly_contributors(prs, windows),
        }
    return build_readings(package=package, repo_data=repo_data, npm_rows=npm_rows,
                          swarm_logins=swarm_logins, now=now)


def _recent_closed_prs(repo: str, cutoff: datetime, fetch: Fetcher) -> Iterable[dict[str, Any]]:
    """Closed PRs most recently updated first, stopping once older than the cutoff."""
    url = f"{GITHUB_API}/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100"
    for item in paginate(url, fetch=fetch):
        if weekly.parse_ts(item["updated_at"]) < cutoff:
            return
        yield item


# --- CLI ---------------------------------------------------------------------------------

UNITS = {"D1_npm_downloads_weekly": "downloads/wk", "D2_github_stars": "stars/wk",
         "D4_external_issues_opened": "issues/wk", "D5_external_contributors": "contributors/28d"}


def env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["compute", "store"])
    parser.add_argument("--npm-package", required=True, help="npm package name (see the product repo's package.json)")
    parser.add_argument("--repo", action="append", help="owner/name; repeatable (or METRICS_REPOS, comma separated)")
    parser.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    parser.add_argument("--swarm-login", action="append", default=[],
                        help="GitHub login of a swarm agent account to exclude (repeatable; also METRICS_SWARM_LOGINS)")
    parser.add_argument("--no-swarm-logins", action="store_true",
                        help="deliberately exclude no agent accounts (agent accounts then count as external)")
    parser.add_argument("--dry-run", action="store_true",
                        help="store: compute and print the target instance and record counts, write nothing")
    args = parser.parse_args(argv)
    swarm_logins = {x.strip().lower() for x in args.swarm_login + env_list("METRICS_SWARM_LOGINS") if x.strip()}
    if not swarm_logins and not args.no_swarm_logins:
        parser.error("name at least one swarm account with --swarm-login or METRICS_SWARM_LOGINS "
                     "(or pass --no-swarm-logins to count agent accounts as external on purpose)")
    repos = args.repo or env_list("METRICS_REPOS")
    if not repos:
        parser.error("name at least one repository with --repo owner/name or METRICS_REPOS")
    now = datetime.now(timezone.utc)
    try:
        readings = collect(package=args.npm_package, repos=repos, weeks=args.weeks,
                           swarm_logins=swarm_logins, now=now)
    except (ValueError, PaginationError) as exc:
        print(f"Refusing to continue: {exc}", file=sys.stderr)
        return 2
    print(f"{len(readings)} readings; window ends {min(r['window_end'] for r in readings)} .. "
          f"{max(r['window_end'] for r in readings)}")
    print(weekly.format_summary(weekly.summarize_readings(readings), UNITS))
    if args.command == "compute":
        return 0
    return neotoma_io.store_command(readings, "repo-baseline", "reading_key", dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
