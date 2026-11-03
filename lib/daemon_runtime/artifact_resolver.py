"""GitHub read-back of a delivered pull request.

A Fly job's result record is the runner's own report of what it delivered. This
module independently reads the real pull request back from GitHub and compares
it with that report: the existence check alone cannot tell the delivered pull
request from a different one that happens to carry the same number.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from urllib.parse import quote

from .artifact_contract import classify_gh_ref_failure

PR_FIELDS = "number,baseRefName,headRefName,headRefOid,headRepository,state,isCrossRepository"


def _gh_pr_view(
    repo: str, number: int, token: str | None = None
) -> tuple[int, str, str]:
    """``gh pr view`` as (returncode, stdout, stderr). Argv only, never a shell.
    The one place this module talks to GitHub; tests replace it.

    With a non-empty *token*, gh authenticates with it (never logged or
    printed); without one, gh uses the ambient credential as before."""
    kwargs = {}
    if isinstance(token, str) and token:
        kwargs["env"] = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}
    proc = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **kwargs,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _gh_repository_view(repo: str, token: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}"], capture_output=True, text=True,
        timeout=30, check=False,
        env={**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token})
    return proc.returncode, proc.stdout or ""


def _gh_branch_view(repo: str, branch: str, token: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token})
    return proc.returncode, proc.stdout or ""



def _gh_rulesets_view(repo: str, token: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/rulesets?includes_parents=true&per_page=100"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token})
    return proc.returncode, proc.stdout or ""


def _gh_ruleset_view(repo: str, number: int, token: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/rulesets/{number}"], capture_output=True,
        text=True, timeout=30, check=False,
        env={**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token})
    return proc.returncode, proc.stdout or ""


def read_back_default_protection(repo: str, token_provider) -> str | None:
    """Require visible existing provider enforcement, never a task boolean.

    Repository-scoped listing includes applicable parent rules. One positively
    qualified active rule suffices; an incomplete/unreadable list never proves
    absence or supplies permission. Redacted bypass is not an empty bypass list.
    The delivery job's normal non-admin issuer contract remains independent.
    """
    try:
        token = token_provider(repo)
        if not isinstance(token, str) or not token:
            return "the designated target-policy read credential is unavailable"
        code, raw = _gh_rulesets_view(repo, token)
        rows = json.loads(raw) if code == 0 else None
        if not isinstance(rows, list):
            return "the applicable default-branch rules could not be read"
        return qualify_default_protection(repo, rows, lambda number: _read_rule(repo, number, token))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return "the existing default-branch protection could not be authoritatively qualified"
    return "no returned rule qualifies active dynamic default protection with explicit empty bypass; hold for the existing target-policy owner"


def _read_rule(repo, number, token):
    code, raw = _gh_ruleset_view(repo, number, token)
    return json.loads(raw) if code == 0 else None


def qualify_default_protection(repo: str, rows, read_rule) -> str | None:
    """Same policy predicate for actual App or directly read owner responses."""
    if not isinstance(rows, list):
        return "the applicable default-branch rules could not be read"
    for summary in rows:
        if not isinstance(summary, dict):
            continue
        number = summary.get("id")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            continue
        if summary.get("target") != "branch" or summary.get("enforcement") != "active":
            continue
        rule = read_rule(number)
        if not isinstance(rule, dict) or any(rule.get(k) != summary.get(k)
                for k in ("id", "source_type", "source", "target", "enforcement")):
            continue
        source_type, source = rule.get("source_type"), rule.get("source")
        if not isinstance(source, str):
            continue
        if source_type == "Repository":
            if source.lower() != repo.lower():
                continue
        elif source_type == "Organization":
            # Membership is established by the selected repository's own
            # inherited-rules listing, never by a generic organization read.
            if source.lower() != repo.split("/", 1)[0].lower():
                continue
        else:
            continue
        conditions = rule.get("conditions")
        refs = conditions.get("ref_name") if isinstance(conditions, dict) else None
        if (not isinstance(refs, dict) or not isinstance(refs.get("include"), list)
                or "~DEFAULT_BRANCH" not in refs["include"] or refs.get("exclude") != []
                or rule.get("bypass_actors") != [] or "bypass_actors" not in rule):
            continue
        if source_type == "Repository" and set(conditions) != {"ref_name"}:
            continue
        if source_type == "Organization":
            names = conditions.get("repository_name")
            if names is not None and (not isinstance(names, dict) or names.get("exclude") != []
                    or not isinstance(names.get("include"), list)
                    or not ({"~ALL", repo.split("/", 1)[1]} & set(names["include"]))):
                continue
            # Unsupported ID/property selectors need owning qualification;
            # do not infer that an arbitrary inherited rule covers this repo.
            if any(k not in {"ref_name", "repository_name"} for k in conditions):
                continue
        rules = rule.get("rules")
        if isinstance(rules, list) and any(isinstance(r, dict) and r.get("type") == "non_fast_forward" for r in rules):
            return None
    return "no returned rule qualifies active dynamic default protection with explicit empty bypass; hold for the existing target-policy owner"


def read_back_delivered_pr(
    *,
    repo,
    number,
    base,
    branch,
    commit,
    attempts: int = 3,
    retry_delay: float = 1.0,
    token_provider=None,
    require_open: bool = False,
) -> str | None:
    """None when the pull request on GitHub is exactly the one reported, else
    a reason naming the field that failed.

    Fails closed: a missing expected value, an unreadable or malformed answer,
    or any difference in head repository, base branch, head branch or head
    commit is a failure. Only a GitHub answer that could not be obtained is
    retried; a 404 is not, since asking again cannot change it.

    With *token_provider*, GitHub is read with ``token_provider(repo)``, asked
    once. If it raises or gives an empty value the delivery is not verified;
    there is no fallback to the ambient credential.
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
    token = None
    if token_provider is not None:
        try:
            token = token_provider(repo)
        except Exception:  # noqa: BLE001 - any failure to mint fails closed
            token = None
        if not isinstance(token, str) or not token:
            return (
                f"{where}: the read-back credential is unavailable; "
                "the delivery is not verified"
            )
    answer = None
    for attempt in range(attempts):
        stderr = ""
        try:
            code, out, stderr = _gh_pr_view(repo, number, token=token)
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
    if require_open and (answer.get("state") != "OPEN" or answer.get("isCrossRepository") is not False):
        return f"{where}: the correction target is not a confirmed open same-repository PR"
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
    if require_open:
        if not isinstance(token, str) or not token:
            return f"{where}: correction source reads need the designated credential"
        try:
            code, raw = _gh_repository_view(repo, token)
            repository = json.loads(raw) if code == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            repository = None
        default = repository.get("default_branch") if isinstance(repository, dict) else None
        name = repository.get("full_name") if isinstance(repository, dict) else None
        if (not isinstance(name, str) or name.lower() != str(repo).lower()
                or not isinstance(default, str) or not default or branch == default):
            return f"{where}: the correction source is not confirmed distinct from the repository default branch"
        try:
            code, raw = _gh_branch_view(repo, branch, token)
            ref = json.loads(raw) if code == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            ref = None
        obj = ref.get("object") if isinstance(ref, dict) else None
        if (not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{branch}"
                or not isinstance(obj, dict) or obj.get("type") != "commit" or obj.get("sha") != commit):
            return f"{where}: the current branch ref is missing or differs from the admitted head"
    return None
