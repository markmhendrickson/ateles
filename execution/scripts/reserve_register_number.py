#!/usr/bin/env python3
"""Reserve a foundation decision or revision number on the origin Git repository.

An issue or Neotoma row cannot atomically create a number on Git's origin server.
A create-only ref can: ``--force-with-lease=<ref>:`` rejects a second claimant
even when neither claimant has pushed its work branch. The ref uses the existing
Git identity and store. Reservations are permanent; abandoned numbers are gaps.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import uuid
from collections import Counter

from foundation_register_rows import decision_rows, revision_rows

KINDS = {"decision": "docs/foundation/conformance.md", "revision": "docs/foundation/revisions.md"}
REF_ROOT = "refs/reservations"
MAX_ATTEMPTS = 20
MAX_RANGE = 200


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def numbers(kind: str, content: str) -> list[int]:
    """Read numbered table rows; a revision may occur in several document tables."""
    if kind == "decision":
        number_cells = decision_rows(content)
    else:
        number_cells = [row.number for row in revision_rows(content)]
    found: list[int] = []
    for cell in number_cells:
        endpoints = re.split("[–-]", cell)
        low = int(endpoints[0])
        high = int(endpoints[1]) if len(endpoints) == 2 else low
        # The register carries range rows ("1-12"); every number in a range is
        # occupied, and a new range must reserve every number it covers.
        if high < low or high - low > MAX_RANGE:
            raise ValueError(f"{kind} row range {low}-{high} is not a valid range")
        found.extend(range(low, high + 1))
    if not found:
        raise ValueError(f"{kind} rows are missing")
    return found


def historical_base_text(kind: str, content: str) -> str:
    """Read the two legacy revision table breaks on origin until their cleanup merges.

    The current worktree is always parsed unchanged. This removes only a blank
    line between two pipe-led rows in the older base copy, not malformed rows.
    Remove this shim once origin/main includes this PR's removal of the two
    blank lines in revisions.md; then both base and worktree use the strict reader.
    """
    if kind == "decision":
        return _escape_legacy_code_span_pipes(content)
    if kind != "revision":
        return content
    return re.sub(r"(?m)(^\|[^\n]*\|\n)\n(?=\|)", r"\1", content)


def _escape_legacy_code_span_pipes(content: str) -> str:
    """Read a base register whose rows carry an unescaped pipe inside a code span.

    GitHub splits a table cell at such a pipe before it reads the code span, so the
    strict reader refuses the row; the base copy on origin still carries one (row
    70). Only table rows (lines that start with a pipe) are touched, and only a pipe
    inside a backtick pair on that line is escaped, so the cell count is what the
    author meant. The current worktree is always parsed unchanged and strict.
    Remove this branch once origin/main includes this PR's escaped row.
    """
    def fix(line: str) -> str:
        if not line.startswith("|"):
            return line
        return re.sub(r"`[^`\n]*`", lambda m: re.sub(r"(?<!\\)\|", r"\\|", m.group(0)), line)

    return "\n".join(fix(line) for line in content.split("\n"))


def table_counts(content: str) -> Counter[tuple[str, int]]:
    """Revision rows per (document table, number). A document's table sits under its own
    `## ` heading; one revision legitimately appears once in EACH document its pass amends."""
    counts: Counter[tuple[str, int]] = Counter()
    for row in revision_rows(content):
        for number in numbers_from_cell(row.number):
            counts[(row.table, number)] += 1
    return counts


def numbers_from_cell(cell: str) -> range:
    endpoints = re.split("[–-]", cell)
    low = int(endpoints[0])
    high = int(endpoints[1]) if len(endpoints) == 2 else low
    if high < low or high - low > MAX_RANGE:
        raise ValueError(f"revision row range {low}-{high} is not a valid range")
    return range(low, high + 1)


def reservations(kind: str, remote: str) -> set[int]:
    prefix = f"{REF_ROOT}/{kind}/"
    output = git("ls-remote", remote, f"{prefix}*")
    found: set[int] = set()
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            raise RuntimeError("git ls-remote returned a malformed reservation row")
        ref = fields[1]
        suffix = ref.removeprefix(prefix)
        if not ref.startswith(prefix) or not suffix.isdecimal():
            raise RuntimeError("git ls-remote returned an unexpected reservation ref")
        found.add(int(suffix))
    return found


def origin_text(kind: str, remote: str) -> tuple[str, str]:
    """Fetch current main before reading its numbered rows."""
    git("fetch", "--no-tags", remote, f"+refs/heads/main:refs/remotes/{remote}/main")
    sha = git("rev-parse", f"refs/remotes/{remote}/main").strip()
    return sha, git("show", f"{sha}:{KINDS[kind]}")


def next_number(kind: str, remote: str) -> tuple[int, str]:
    sha, content = origin_text(kind, remote)
    occupied = numbers(kind, historical_base_text(kind, content)) + list(reservations(kind, remote))
    return max(occupied) + 1, sha


def claim_object(kind: str, number: int, base: str) -> str:
    """A commit that is unique to this claim.

    Pushing the SAME object to an existing ref is "Everything up-to-date" and
    exits 0, so two claimants that pushed the shared origin/main commit would
    both believe they hold the number. A distinct object per claim makes the
    server-side create-only lease refuse the second claimant (verified against
    github.com: a different commit to an existing reservation ref is rejected
    with "stale info", the same commit is not).
    """
    tree = git("rev-parse", f"{base}^{{tree}}").strip()
    message = f"register reservation {kind} {number} {uuid.uuid4().hex}\n"
    env = {**os.environ, "GIT_AUTHOR_NAME": "register-reservation", "GIT_AUTHOR_EMAIL": "reservation@invalid",
           "GIT_COMMITTER_NAME": "register-reservation", "GIT_COMMITTER_EMAIL": "reservation@invalid"}
    result = subprocess.run(["git", "commit-tree", tree, "-p", base], input=message, env=env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git commit-tree failed: {result.stderr.strip()}")
    return result.stdout.strip()


def claim(kind: str, remote: str, number: int, base: str) -> tuple[bool, str]:
    """One create-only attempt on a number. True only when THIS claimant created the ref."""
    ref = f"{REF_ROOT}/{kind}/{number}"
    obj = claim_object(kind, number, base)
    result = subprocess.run(
        ["git", "push", f"--force-with-lease={ref}:", remote, f"{obj}:{ref}"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0, result.stderr.strip()


def reserve(kind: str, remote: str, attempts: int = MAX_ATTEMPTS) -> int:
    for _ in range(attempts):
        candidate, sha = next_number(kind, remote)
        won, stderr = claim(kind, remote, candidate, sha)
        if won:
            return candidate
        # A rejected lease is expected under contention. Fail closed if origin
        # remains unreadable or the failed push did not create this ref.
        if candidate not in reservations(kind, remote):
            raise RuntimeError(f"reservation push failed: {stderr}")
    raise RuntimeError(f"reservation contention exceeded {attempts} attempts")


def check(kind: str, remote: str, base: str) -> list[str]:
    """Check numbers introduced by the worktree against create-only refs."""
    source = KINDS[kind]
    old_text = historical_base_text(kind, git("show", f"{base}:{source}"))
    before = numbers(kind, old_text)
    from pathlib import Path
    after = numbers(kind, Path(source).read_text(encoding="utf-8"))
    errors: list[str] = []
    if kind == "revision":
        # One row per revision per document table: a number may not repeat INSIDE one table
        # (across different tables it may). Rows already doubled on the base are history.
        new_text = Path(source).read_text(encoding="utf-8")
        tables_before, tables_after = table_counts(old_text), table_counts(new_text)
        for (title, number), count in sorted(tables_after.items()):
            if count > 1 and count > tables_before.get((title, number), 0):
                errors.append(f"revision {number} appears {count} times in the table under {title!r}; "
                              "one row per revision per document")
    counts_before = Counter(before)
    counts_after = Counter(after)
    held = reservations(kind, remote)
    for number in sorted(counts_after):
        gained = counts_after[number] - counts_before.get(number, 0)
        if gained <= 0:
            continue
        if kind == "decision" and counts_after[number] > 1:
            errors.append(f"duplicate decision row number {number} in the register")
        elif number in counts_before:
            # Numbers are never reused (revisions.md): a pass that amends several
            # documents adds its rows under ONE number in the same change, which
            # is a number NEW to origin/main, never one already there.
            errors.append(f"{kind} {number} already exists on {base}; a number is never reused")
        elif number not in held:
            errors.append(
                f"{kind} {number} has no reservation; run "
                f"python3 execution/scripts/reserve_register_number.py reserve {kind} "
                f"and use the number it prints"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("next", "reserve", "check"))
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    try:
        if args.action == "next":
            value, _ = next_number(args.kind, args.remote)
            print(value)
        elif args.action == "reserve":
            print(reserve(args.kind, args.remote))
        else:
            errors = check(args.kind, args.remote, args.base)
            for error in errors:
                print(error, file=sys.stderr)
            if errors:
                return 1
            print(f"{args.kind} reservations: checked")
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"{args.kind} reservation check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
