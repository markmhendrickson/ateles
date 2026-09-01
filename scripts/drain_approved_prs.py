#!/usr/bin/env python3
"""One-shot drain for APPROVED-but-unmerged PRs (ateles#511).

Measured 2026-08-31: 4 open PRs sat APPROVED and unmerged for 12/33/47/53 days,
and three had rotted CLEAN -> CONFLICTING through nothing but elapsed time. An
approved PR *presents as done*, which is why nothing was watching it — unlike
CHANGES_REQUESTED, which at least presents as needing work.

This script NEVER merges. Per #511 merging stays behind the existing merge
gates and the operator's call; what this does is classify each approved PR by
WHY it is still open, and record a durable hold marker on the ones that cannot
proceed on their own:

    <!-- apis-approved-hold:<reason> -->

so the next reader sees the reason on the thread instead of rediscovering it.

    python3 scripts/drain_approved_prs.py --repo markmhendrickson/ateles
    python3 scripts/drain_approved_prs.py --repo o/r --apply
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone

HOLD_MARKER = "<!-- apis-approved-hold:{reason} -->"

# mergeStateStatus -> (hold reason, what unblocks it)
_STATES: dict[str, tuple[str, str]] = {
    "CLEAN": ("", "mergeable now — merge is the only remaining step"),
    "DIRTY": ("conflict", "already rotted; needs a rebase before it can merge"),
    "BLOCKED": ("required-check", "a required check or branch protection is holding it"),
    "BEHIND": ("behind-base", "base moved; needs an update but no conflict yet"),
    "UNSTABLE": ("failing-check", "a non-required check is failing"),
    "UNKNOWN": ("unknown", "GitHub has not computed mergeability yet"),
}


def _gh(args: list[str]) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def classify(pr: dict) -> tuple[str, str]:
    """(hold_reason, human explanation). An empty reason means no hold needed."""
    return _STATES.get(pr.get("mergeStateStatus") or "UNKNOWN", _STATES["UNKNOWN"])


def list_approved(repo: str) -> list[dict]:
    """Open PRs whose standing review decision is APPROVED."""
    raw = _gh(
        [
            "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
            "--json", "number,title,reviewDecision,mergeStateStatus,updatedAt,url",
        ]
    )
    out = []
    for pr in json.loads(raw or "[]"):
        if pr.get("reviewDecision") != "APPROVED":
            continue
        updated = _parse_ts(pr.get("updatedAt", ""))
        pr["_stale_days"] = (
            (datetime.now(timezone.utc) - updated).days if updated else -1
        )
        out.append(pr)
    return sorted(out, key=lambda p: p["_stale_days"], reverse=True)


def has_hold(repo: str, number: int, reason: str) -> bool:
    """True when this same hold reason already stands on the thread."""
    try:
        raw = _gh(["pr", "view", str(number), "--repo", repo, "--json", "comments"])
    except subprocess.CalledProcessError:
        return True  # cannot tell -> do not double-post
    body = " ".join(c.get("body", "") for c in json.loads(raw or "{}").get("comments", []))
    return HOLD_MARKER.format(reason=reason) in body


def record_hold(repo: str, number: int, reason: str, detail: str, *, apply: bool) -> str:
    if not apply:
        return f"DRY RUN: would hold {repo}#{number} ({reason})"
    if has_hold(repo, number, reason):
        return f"SKIPPED {repo}#{number}: this hold already stands"
    body = (
        f"{HOLD_MARKER.format(reason=reason)}\n"
        f"**Approved, not merged — held: `{reason}`**\n\n{detail}\n\n"
        "Recorded by the approved-PR drain (ateles#511). This script never "
        "merges; merging stays behind the existing merge gates."
    )
    _gh(["pr", "comment", str(number), "--repo", repo, "--body", body])
    return f"held {repo}#{number} ({reason})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument(
        "--apply", action="store_true", help="post hold markers. Default is a dry run."
    )
    args = p.parse_args(argv)

    approved = list_approved(args.repo)
    if not approved:
        print("No open APPROVED PRs.")
        return 0

    ready, held = [], []
    for pr in approved:
        reason, detail = classify(pr)
        (ready if not reason else held).append((pr, reason, detail))

    print(f"{len(approved)} approved, unmerged PR(s):\n")
    for pr, reason, detail in ready:
        print(f"  #{pr['number']:<5} {pr['_stale_days']:>3}d  MERGE-READY   {pr['title'][:56]}")
    for pr, reason, detail in held:
        print(f"  #{pr['number']:<5} {pr['_stale_days']:>3}d  {reason:<13} {pr['title'][:56]}")
        print(f"        -> {record_hold(args.repo, pr['number'], reason, detail, apply=args.apply)}")

    if ready:
        print(
            f"\n{len(ready)} PR(s) are merge-ready. This script does not merge them: "
            "hand them to the existing merge path, or merge them yourself."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
