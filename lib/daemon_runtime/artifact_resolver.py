"""GitHub read-back of a delivered pull request.

A Fly job's result record is the runner's own report of what it delivered. This
module independently reads the real pull request back from GitHub and compares
it with that report: the existence check alone cannot tell the delivered pull
request from a different one that happens to carry the same number.
"""

from __future__ import annotations

import json
import subprocess
import time

from .artifact_contract import classify_gh_ref_failure

PR_FIELDS = "number,baseRefName,headRefName,headRefOid,headRepository"


def _gh_pr_view(repo: str, number: int) -> tuple[int, str, str]:
    """``gh pr view`` as (returncode, stdout, stderr). Argv only, never a shell.
    The one place this module talks to GitHub; tests replace it."""
    proc = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def read_back_delivered_pr(
    *, repo, number, base, branch, commit, attempts: int = 3, retry_delay: float = 1.0
) -> str | None:
    """None when the pull request on GitHub is exactly the one reported, else
    a reason naming the field that failed.

    Fails closed: a missing expected value, an unreadable or malformed answer,
    or any difference in head repository, base branch, head branch or head
    commit is a failure. Only a GitHub answer that could not be obtained is
    retried; a 404 is not, since asking again cannot change it.
    """
    expected = {
        "repository": repo,
        "pull request number": number,
        "base branch": base,
        "head branch": branch,
        "head commit": commit,
    }
    for name, value in expected.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            return f"the runner's result did not report the {name}; the delivery is not verified"
    where = f"{repo}#{number}"
    answer = None
    for attempt in range(attempts):
        stderr = ""
        try:
            code, out, stderr = _gh_pr_view(repo, number)
        except (OSError, subprocess.TimeoutExpired):
            code, out = 1, ""
        if classify_gh_ref_failure(code, stderr) == "exists":
            try:
                answer = json.loads(out)
            except ValueError:
                answer = None
            break
        if "http 404" in stderr.lower():
            break
        if attempt + 1 < attempts:
            time.sleep(retry_delay)
    if not isinstance(answer, dict):
        return (
            f"{where} could not be read back from GitHub; the delivery is not verified"
        )
    head_repo = answer.get("headRepository")
    actual = {
        "repository": head_repo.get("nameWithOwner")
        if isinstance(head_repo, dict)
        else None,
        "pull request number": answer.get("number"),
        "base branch": answer.get("baseRefName"),
        "head branch": answer.get("headRefName"),
        "head commit": answer.get("headRefOid"),
    }
    for name, want in expected.items():
        got = actual[name]
        if name == "repository":
            same = isinstance(got, str) and got.lower() == str(want).lower()
        else:
            same = got == want
        if not same:
            return (
                f"{where} does not match the runner's report: {name} is "
                f"{got!r} on GitHub, expected {want!r}"
            )
    return None
