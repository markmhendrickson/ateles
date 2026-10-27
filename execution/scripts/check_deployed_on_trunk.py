#!/usr/bin/env python3
"""After-the-fact audit: is the commit that is deployed an ancestor of trunk?

Foundation decision 148 says an application's delivery never depends on swarm
coordination; the companion rule for reviewers is that the head they reviewed
is the head that is live. A deploy from an unmerged integration branch breaks
that silently. `.github/actions/deployed-on-trunk` stops it at deploy time;
this script audits it after the fact, from outside the deploy workflow.

CAVEAT: this is a weaker, later check. Ancestry only grows, so it is judged as
of NOW and cannot tell deployed-then-merged from merged-then-deployed: a commit
deployed from an unmerged branch and merged later reads as on trunk. It is not
proof that what was deployed had been reviewed on trunk; the deploy-time guard
is the check that binds. (A Deployment record's `ref` is the commit sha and is
provenance only.)

Where the deployed sha comes from (exactly one):
    --sha SHA                       a commit you already know
    --from-url URL --json-field F   a version/health endpoint, e.g. /version
                                    returning {"commit": "<sha>"}; F may be a
                                    dotted path such as "build.commit"
    --from-deployments ENV          the latest SUCCESSFUL GitHub Deployment
                                    for environment ENV (the deploy workflow
                                    in templates/app-ci records one per deploy)

The check uses the GitHub compare API, `repos/{repo}/compare/{branch}...{sha}`.
Status `behind` or `identical` means the sha is reachable from the branch tip
(on trunk). `ahead` or `diverged` means it is not.

Usage:
    python3 execution/scripts/check_deployed_on_trunk.py --repo OWNER/NAME --sha SHA
    python3 execution/scripts/check_deployed_on_trunk.py --repo OWNER/NAME \\
        --from-url https://app.example/version --json-field commit
    python3 execution/scripts/check_deployed_on_trunk.py --repo OWNER/NAME \\
        --from-deployments production

Exit codes:
    0  the deployed commit is on trunk
    1  the deployed commit is NOT on trunk
    2  the check could not run (bad input, network failure, auth failure,
       unknown repo/sha, no successful deployment, unreadable endpoint).
       Exit 2 is "unknown" and must never be reported as a pass.

--from-url verifies what the running service SAYS its commit is. A service
that reports a wrong or stale value passes this check; it does not inspect the
image or the code. The deployment record (--from-deployments) is the second,
independent source.

Prints exactly one plain line. Stdlib only. Authenticates with GH_TOKEN (or
GITHUB_TOKEN) when set, never prints it, and never follows a redirect on
api.github.com calls so the token cannot reach another host. Without a token,
only public repos are readable.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 20
# Full 40-hex shas only: a short prefix can be ambiguous, and an endpoint that
# reports a prefix should be fixed to report the whole commit.
SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")

EXIT_ON_TRUNK = 0
EXIT_NOT_ON_TRUNK = 1
EXIT_COULD_NOT_RUN = 2

# Statuses the compare API reports for `branch...sha`.
ON_TRUNK_STATUSES = frozenset({"behind", "identical"})
OFF_TRUNK_STATUSES = frozenset({"ahead", "diverged"})

# How many recent deployments to walk looking for a successful one.
DEPLOYMENTS_PAGE_SIZE = 30


class CheckError(Exception):
    """The check could not run. Always maps to exit 2, never to a pass."""


def _token() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so a 3xx surfaces as an HTTPError instead of
    being followed with the Authorization header attached."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(url: str, headers: dict[str, str], follow_redirects: bool = True):
    request = urllib.request.Request(url, headers=headers)
    if follow_redirects:
        return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    return _NO_REDIRECT_OPENER.open(request, timeout=TIMEOUT_SECONDS)


def api_get(path: str):
    """GET a GitHub API path and return parsed JSON. Raises CheckError."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ateles-check-deployed-on-trunk",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = API_ROOT + path
    try:
        # api.github.com never needs a redirect here; refusing them keeps the
        # token from being forwarded to any other host.
        with _open(url, headers, follow_redirects=False) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CheckError(
                f"GitHub returned 404 for {path.split('?')[0]}: the repo, branch "
                "or commit is unknown to GitHub, or the token cannot see the repo"
            ) from exc
        if exc.code in (401, 403):
            raise CheckError(
                f"GitHub refused access (HTTP {exc.code}); check GH_TOKEN scope or rate limit"
            ) from exc
        raise CheckError(f"GitHub API error HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise CheckError(f"network error reaching GitHub: {exc}") from exc
    except ValueError as exc:
        raise CheckError("GitHub returned a response that is not JSON") from exc


def fetch_json_url(url: str):
    """GET an arbitrary version/health endpoint and parse its JSON body."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CheckError(f"--from-url must be an http(s) URL, got {url!r}")
    try:
        with _open(url, {"Accept": "application/json", "User-Agent": "ateles-check-deployed-on-trunk"}) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CheckError(f"version endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise CheckError(f"network error reaching version endpoint: {exc}") from exc
    except ValueError as exc:
        raise CheckError("version endpoint did not return JSON") from exc


def extract_field(payload, field: str) -> str:
    """Read a (dotted) field out of a JSON object; require a sha-shaped string."""
    value = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            raise CheckError(f"field {field!r} not found in the version endpoint response")
        value = value[part]
    if not isinstance(value, str) or not SHA_RE.fullmatch(value.strip()):
        raise CheckError(f"field {field!r} does not hold a commit sha")
    return value.strip()


def latest_successful_deployment_sha(repo: str, environment: str) -> str:
    """Return the sha of the newest deployment in `environment` whose latest
    status is `success`. Deployments are listed newest first."""
    query = urllib.parse.urlencode({"environment": environment, "per_page": DEPLOYMENTS_PAGE_SIZE})
    deployments = api_get(f"/repos/{repo}/deployments?{query}")
    if not isinstance(deployments, list):
        raise CheckError("unexpected deployments response from GitHub")
    for deployment in deployments:
        if not isinstance(deployment, dict):
            raise CheckError("unexpected deployments response from GitHub")
        deployment_id = deployment.get("id")
        sha = deployment.get("sha")
        if deployment_id is None or not sha:
            continue
        statuses = api_get(f"/repos/{repo}/deployments/{deployment_id}/statuses?per_page=1")
        # Statuses are newest first; only the latest decides the state.
        if not isinstance(statuses, list) or any(not isinstance(item, dict) for item in statuses):
            raise CheckError("unexpected deployment statuses response from GitHub")
        if statuses and statuses[0].get("state") == "success":
            return sha
    raise CheckError(
        f"no successful deployment found for environment {environment!r} in the "
        f"latest {DEPLOYMENTS_PAGE_SIZE} deployments"
    )


def default_branch(repo: str) -> str:
    info = api_get(f"/repos/{repo}")
    branch = info.get("default_branch") if isinstance(info, dict) else None
    if not branch:
        raise CheckError("could not determine the repo's default branch; pass --branch")
    return branch


def compare_status(repo: str, branch: str, sha: str) -> str:
    quoted_branch = urllib.parse.quote(branch, safe="")
    result = api_get(f"/repos/{repo}/compare/{quoted_branch}...{sha}?per_page=1")
    status = result.get("status") if isinstance(result, dict) else None
    if not status:
        raise CheckError("compare response carried no status")
    return status


def resolve_sha(args: argparse.Namespace) -> tuple[str, str]:
    """Return (sha, human description of where it came from)."""
    if args.sha:
        sha = args.sha.strip()
        source = "given sha"
    elif args.from_url:
        if not args.json_field:
            raise CheckError("--from-url requires --json-field")
        sha = extract_field(fetch_json_url(args.from_url), args.json_field)
        source = f"{args.from_url} ({args.json_field})"
    else:
        sha = latest_successful_deployment_sha(args.repo, args.from_deployments)
        source = f"latest successful {args.from_deployments} deployment"
    if not SHA_RE.fullmatch(sha):
        raise CheckError(f"{sha!r} is not a commit sha")
    return sha, source


def run(args: argparse.Namespace) -> tuple[int, str]:
    try:
        sha, source = resolve_sha(args)
        branch = args.branch or default_branch(args.repo)
        status = compare_status(args.repo, branch, sha)
    except CheckError as exc:
        return EXIT_COULD_NOT_RUN, f"UNKNOWN: could not check ({exc})"
    short = sha[:12]
    if status in ON_TRUNK_STATUSES:
        return EXIT_ON_TRUNK, f"ON TRUNK: {short} from {source} is an ancestor of {branch} in {args.repo}"
    if status in OFF_TRUNK_STATUSES:
        relation = f"ahead of {branch}" if status == "ahead" else f"diverged from {branch}"
        return (
            EXIT_NOT_ON_TRUNK,
            f"NOT ON TRUNK: {short} from {source} is {relation} in {args.repo}; "
            f"merge it to {branch} first, or deploy a commit that is already on {branch}",
        )
    return EXIT_COULD_NOT_RUN, f"UNKNOWN: could not check (unrecognised compare status {status!r})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="owner/name of the repository")
    parser.add_argument("--branch", help="trunk branch (default: the repo's default branch)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sha", help="deployed commit sha")
    source.add_argument("--from-url", help="version/health endpoint returning JSON that holds the sha")
    source.add_argument(
        "--from-deployments",
        metavar="ENV",
        help="read the sha of the latest successful GitHub Deployment for environment ENV",
    )
    parser.add_argument("--json-field", help="field holding the sha in the --from-url response (dotted path)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not REPO_RE.fullmatch(args.repo):
        print(f"UNKNOWN: could not check (--repo must be owner/name, got {args.repo!r})")
        return EXIT_COULD_NOT_RUN
    try:
        code, line = run(args)
    except Exception as exc:  # noqa: BLE001 - exit 2 is the contract for every failure
        # Name only the exception type: its message could echo a URL or header.
        code, line = EXIT_COULD_NOT_RUN, f"UNKNOWN: could not check ({type(exc).__name__})"
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
