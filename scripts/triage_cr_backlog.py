#!/usr/bin/env python3
"""One-shot triage for the pre-cutover CHANGES_REQUESTED backlog (ateles#511).

Measured 2026-08-31: 23 of 47 open ateles PRs sat at CHANGES_REQUESTED, median
33 days, oldest 67. `resume_unactioned_revisions` deliberately does not touch
them — it keys on a PUSH, so a PR whose author never revised is abandoned work
rather than a carrier failure, and re-arming it would bury that fact under a
fresh review round.

This is the other half: a human-or-agent decision, recorded once per PR, on
whether each stale PR is revived or closed. Per #511 there is NO silent bulk
re-dispatch — the script's whole job is to make each verdict explicit and
durable on the PR thread:

    <!-- apis-revision-verdict:revive -->   -> becomes eligible for the sweep
    <!-- apis-revision-verdict:close -->    -> closed, with the reason recorded

Default is a DRY RUN that lists candidates and the verdict each would receive.
Nothing is posted or closed without --apply, and a PR that already carries a
verdict marker is never given a second one.

    python3 scripts/triage_cr_backlog.py --repo markmhendrickson/ateles
    python3 scripts/triage_cr_backlog.py --repo o/r --number 592 \
        --verdict revive --reason "author is actively pushing" --apply
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

# Named in code per #511: PRs opened before the swarm's review cutover are the
# ones whose verdicts predate the current panel and cannot be trusted as-is.
CUTOVER = datetime(2026, 8, 4, tzinfo=timezone.utc)

REVIVE_MARKER = "<!-- apis-revision-verdict:revive -->"
CLOSE_MARKER = "<!-- apis-revision-verdict:close -->"
_MARKERS = (REVIVE_MARKER, CLOSE_MARKER)


def _gh(args: list[str]) -> str:
    """Run gh and return stdout. Raises CalledProcessError on failure."""
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def list_candidates(repo: str, cutover: datetime = CUTOVER) -> list[dict]:
    """Open PRs at CHANGES_REQUESTED created before the cutover, without a verdict.

    A PR that already carries a verdict marker is excluded: #511 asks for
    exactly one verdict per PR, and re-posting would make the thread ambiguous
    about which decision stands.
    """
    raw = _gh(
        [
            "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
            "--json", "number,title,createdAt,reviewDecision,url",
        ]
    )
    out: list[dict] = []
    for pr in json.loads(raw or "[]"):
        if pr.get("reviewDecision") != "CHANGES_REQUESTED":
            continue
        created = _parse_ts(pr.get("createdAt", ""))
        if created is None or created >= cutover:
            continue
        if has_verdict(repo, int(pr["number"])):
            continue
        pr["_age_days"] = (datetime.now(timezone.utc) - created).days
        out.append(pr)
    return sorted(out, key=lambda p: p["_age_days"], reverse=True)


def has_verdict(repo: str, number: int) -> bool:
    """True when a verdict marker already stands on the PR thread."""
    try:
        raw = _gh(["pr", "view", str(number), "--repo", repo, "--json", "comments"])
    except subprocess.CalledProcessError:
        # Cannot tell -> assume a verdict exists. A false positive skips one PR;
        # a false negative posts a second, contradictory verdict.
        return True
    body = " ".join(c.get("body", "") for c in json.loads(raw or "{}").get("comments", []))
    return any(marker in body for marker in _MARKERS)


def record_verdict(
    repo: str, number: int, verdict: str, reason: str, *, apply: bool
) -> str:
    """Post exactly one verdict comment; close the PR when the verdict is close."""
    if verdict not in ("revive", "close"):
        raise ValueError(f"verdict must be 'revive' or 'close', got {verdict!r}")
    if not reason.strip():
        raise ValueError("a verdict needs a reason — an unexplained close is not triage")

    marker = REVIVE_MARKER if verdict == "revive" else CLOSE_MARKER
    body = (
        f"{marker}\n"
        f"**Backlog triage — {verdict.upper()}**\n\n{reason.strip()}\n\n"
        + (
            "This PR is eligible for the revision sweep again; a push will "
            "re-dispatch the fix agent against the outstanding findings."
            if verdict == "revive"
            else "Closing as part of the pre-cutover backlog triage (ateles#511). "
            "Reopen if this is still wanted."
        )
    )
    if not apply:
        return f"DRY RUN: would record {verdict} on {repo}#{number}"

    if has_verdict(repo, number):
        return f"SKIPPED {repo}#{number}: a verdict already stands"

    _gh(["pr", "comment", str(number), "--repo", repo, "--body", body])
    if verdict == "close":
        _gh(["pr", "close", str(number), "--repo", repo])
    return f"recorded {verdict} on {repo}#{number}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--number", type=int, help="record a verdict on one PR")
    p.add_argument("--verdict", choices=("revive", "close"))
    p.add_argument("--reason", default="")
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually post/close. Without it this is a dry run.",
    )
    args = p.parse_args(argv)

    if args.number:
        if not args.verdict:
            p.error("--verdict is required with --number")
        try:
            print(
                record_verdict(
                    args.repo, args.number, args.verdict, args.reason, apply=args.apply
                )
            )
        except ValueError as exc:
            p.error(str(exc))
        return 0

    candidates = list_candidates(args.repo)
    if not candidates:
        print("No untriaged pre-cutover CHANGES_REQUESTED PRs.")
        return 0

    print(f"{len(candidates)} PR(s) awaiting a triage verdict:\n")
    for pr in candidates:
        print(f"  #{pr['number']:<5} {pr['_age_days']:>3}d  {pr['title'][:70]}")
    print(
        "\nRecord one verdict per PR (nothing is bulk-dispatched):\n"
        f"  python3 {sys.argv[0]} --repo {args.repo} --number <N> "
        "--verdict revive|close --reason '...' --apply"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
