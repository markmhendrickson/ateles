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

KINDS = {"decision": "docs/foundation/conformance.md", "revision": "docs/foundation/revisions.md"}
REF_ROOT = "refs/reservations"
MAX_ATTEMPTS = 20


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def numbers(kind: str, content: str) -> list[int]:
    """Read numbered table rows; a revision may occur in several document tables."""
    if kind == "decision":
        header = "| # | The question |"
        if header not in content:
            raise ValueError("decision register table is missing")
        content = content.split(header, 1)[1]
    found = [int(match.group(1)) for match in re.finditer(r"^\|\s*(\d+)\s*\|", content, re.M)]
    if not found:
        raise ValueError(f"{kind} rows are missing")
    return found


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
    occupied = numbers(kind, content) + list(reservations(kind, remote))
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
    before = numbers(kind, git("show", f"{base}:{source}"))
    from pathlib import Path
    after = numbers(kind, Path(source).read_text(encoding="utf-8"))
    errors: list[str] = []
    if kind == "decision" and len(after) != len(set(after)):
        errors.append("duplicate decision row number in the register")
    added = set(after) - set(before)
    held = reservations(kind, remote)
    for number in sorted(added):
        if number not in held:
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
