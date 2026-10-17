"""GitHub I/O for the existing artifact contract's tri-state resolver."""

import json
import time

from .artifact_contract import ParsedRef, classify_gh_ref_failure


def resolve_artifact_ref(parsed: ParsedRef, *, repo: str | None = None,
                         expected_pr: dict | None = None, attempts: int = 3,
                         retry_delay: float = 1.0) -> str:
    """Ask GitHub whether a parsed PR/commit exists, via argv ``gh``.

    Never ``shell=True`` and never an HTTP probe. Returns a TRI-STATE string:
    ``"exists"``, ``"absent"`` (GitHub answered that it is not there), or
    ``"unavailable"`` (a rate limit, 5xx, auth error, timeout, OSError or any
    non-zero exit that is not a clear not-found). "Unavailable" is not "absent":
    it says nothing about the ref, and calling it absent would mark a task that
    has a real PR FAILED, which the stall watchdog re-runs (a second PR).

    Injectable: tests monkeypatch this to avoid live GitHub calls; a bool return
    from a stub is read as exists / absent (see `_normalize_ref_check`).
    """
    import subprocess

    owner = parsed.owner or ""
    name = parsed.repo or ""
    if repo and "/" in repo and (not owner or not name):
        owner, name = repo.split("/", 1)
    if parsed.kind == "pr":
        if not owner or not name or parsed.number is None:
            return "unavailable"
        cmd = [
            "gh", "pr", "view", str(parsed.number),
            "--repo", f"{owner}/{name}", "--json",
            "number,url,baseRefName,headRefName,headRefOid,headRepository" if expected_pr else "number",
        ]
    elif parsed.kind == "sha":
        if not owner or not name or not parsed.sha:
            return "unavailable"
        cmd = [
            "gh", "api", f"repos/{owner}/{name}/commits/{parsed.sha}",
            "--jq", ".sha",
        ]
    else:
        return "unavailable"
    verdict = "unavailable"
    for attempt in range(attempts):
        stderr = ""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False
            )
            stderr = proc.stderr or ""
            verdict = classify_gh_ref_failure(proc.returncode, stderr)
            if verdict == "exists" and expected_pr is not None:
                try:
                    answer = json.loads(proc.stdout)
                    target = f"{owner}/{name}".lower()
                    matches = (
                        answer.get("number") == parsed.number
                        and isinstance(answer.get("url"), str)
                        and answer["url"].lower() == f"https://github.com/{target}/pull/{parsed.number}"
                        and answer.get("baseRefName") == expected_pr["base"]
                        and answer.get("headRefName") == expected_pr["branch"]
                        and answer.get("headRefOid") == expected_pr["commit"]
                        and isinstance(answer.get("headRepository"), dict)
                        and str(answer["headRepository"].get("nameWithOwner", "")).lower() == target
                    )
                    verdict = "exists" if matches else "absent"
                except (ValueError, TypeError, AttributeError, KeyError):
                    verdict = "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            verdict = "unavailable"
        if verdict != "unavailable":
            break
        # A 404 means the repository is missing or not visible to this identity;
        # asking again a second later cannot change that.
        if "http 404" in stderr.lower():
            break
        if attempt + 1 < attempts:
            time.sleep(retry_delay)
    return verdict

