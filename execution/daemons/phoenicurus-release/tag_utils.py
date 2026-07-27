#!/usr/bin/env python3
"""
Calendar-tag and release-guard logic for the Ateles release workflow
(.github/workflows/ateles-release.yml).

Pulled out of inline bash so it has unit test coverage (test_tag_utils.py) —
a regression here silently changes what gets tagged in production with no CI
signal. Git-agnostic: callers supply the set of existing tags / commit counts
they already have from `git`, rather than this module shelling out itself.

CLI usage (thin wrapper the workflow calls):
  python3 tag_utils.py compute-tag --today YYYY.MM.DD --existing-tags "$(git tag)"
  python3 tag_utils.py has-releasable-commits --count N [--prev TAG]
"""

from __future__ import annotations

import argparse
import sys


def next_calendar_tag(existing_tags: set[str], today: str) -> str:
    """
    Compute the next calendar tag (vYYYY.MM.DD[.N]) for `today` given the set
    of tag names that already exist. Several merges can land on one day, so
    the first free `.N` suffix after the bare date is used.
    """
    base = f"v{today}"
    tag = base
    n = 1
    while tag in existing_tags:
        tag = f"{base}.{n}"
        n += 1
    return tag


def has_releasable_commits(prev_tag: str | None, non_merge_commit_count: int) -> bool:
    """
    True if there is something worth cutting a release for. With no previous
    tag, treat the repo as always releasable regardless of commit count (an
    initial release always proceeds). Otherwise require at least one
    non-merge commit since the previous tag.
    """
    if not prev_tag:
        return True
    return non_merge_commit_count != 0


def _cmd_compute_tag(args: argparse.Namespace) -> None:
    existing = {line.strip() for line in args.existing_tags.splitlines() if line.strip()}
    print(next_calendar_tag(existing, args.today))


def _cmd_has_releasable_commits(args: argparse.Namespace) -> None:
    result = has_releasable_commits(args.prev or None, args.count)
    print("true" if result else "false")
    sys.exit(0 if result else 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ateles release tag/versioning helpers")
    sub = ap.add_subparsers(dest="command", required=True)

    p_tag = sub.add_parser("compute-tag", help="print the next calendar tag")
    p_tag.add_argument("--today", required=True, help="today's date as YYYY.MM.DD")
    p_tag.add_argument(
        "--existing-tags", default="", help="newline-separated existing tag names"
    )
    p_tag.set_defaults(func=_cmd_compute_tag)

    p_check = sub.add_parser(
        "has-releasable-commits", help="exit 0/print true iff there is something to release"
    )
    p_check.add_argument("--count", type=int, required=True, help="non-merge commit count")
    p_check.add_argument("--prev", default="", help="previous tag, empty if none")
    p_check.set_defaults(func=_cmd_has_releasable_commits)

    args = ap.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
