#!/usr/bin/env python3
"""Prove this repository's public rule sources parse, with no private store.

ateles#1333 split the canonical rule inventory in two. Complete cross-store
measurement -- the entity stores in Neotoma prod, the operator's harness files,
the other repositories on the operator's host -- is a private, milestone-driven
audit run locally (`docs/runbooks/rule_inventory_audit.md`). What a pull request
can change, and what a GitHub-hosted runner can therefore check, is the part of
the rule estate that lives in this public repository:

- `CLAUDE.md`, the session instruction file;
- `.claude/skills/*/SKILL.md`, the repository's skills;
- `.claude/hooks/*.py`, rules stated as enforcement code.

This check runs the rule inventory's OWN file-store reader
(`render_rule_inventory.read_file_stores`) over this checkout with an empty,
temporary home directory and the canonical-repository-roots variable removed,
so no private store is reachable even when it runs on the operator's host. It
then requires each public store to be present, read, populated, and to yield at
least one rule statement. Zero statements from a store that is known to state
rules is a broken instrument or a broken source, never a clean pass
(`CLAUDE.md`, "Validate the instrument before believing the measurement").

It prints store names and counts only -- never a rule's text or a path outside
this repository.

Exit codes:
    0  every public rule source parsed and yielded rules
    1  a public rule source is missing, unread, empty, or yielded no rules,
       or a store outside the public set was read
    2  the check could not run (the reader raised) -- not the same as passing

Usage:
    python3 execution/scripts/check_public_rule_sources.py
    python3 execution/scripts/check_public_rule_sources.py --root <checkout>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import render_rule_inventory as renderer

REPO_ROOT = Path(__file__).resolve().parents[2]

# The rule stores that live in this public repository. Every other store the
# inventory knows is private and belongs to the milestone audit.
PUBLIC_RULE_STORES = (
    "ateles/CLAUDE.md",
    "Skills (ateles repo)",
    "Claude Code hooks (ateles)",
)

# Read as unavailable (never populated) when no private configuration exists;
# it is the aggregate of the operator's other repositories.
EXPECTED_UNREAD = frozenset({"Canonical repository instruction roots"})


def measure_public_stores(root: Path) -> list[renderer.Store]:
    """Read the file stores of ``root`` with no private store reachable."""
    saved = os.environ.pop(renderer.CANONICAL_REPOSITORY_ROOTS_ENV, None)
    try:
        with tempfile.TemporaryDirectory() as empty_home:
            _statements, stores = renderer.read_file_stores(
                Path(empty_home), repository_input_root=root
            )
    finally:
        if saved is not None:
            os.environ[renderer.CANONICAL_REPOSITORY_ROOTS_ENV] = saved
    return stores


def problems_in(stores: list[renderer.Store]) -> list[str]:
    """Return one line per violated expectation; empty means the sources parse."""
    problems: list[str] = []
    by_name = {store.name: store for store in stores}
    for name in PUBLIC_RULE_STORES:
        store = by_name.get(name)
        if store is None:
            problems.append(f"{name}: not found")
            continue
        if not store.read_ok:
            problems.append(f"{name}: unread")
        if store.populated < 1:
            problems.append(f"{name}: no files")
        if store.statements < 1:
            problems.append(f"{name}: parsed to zero rule statements")
    for store in stores:
        if store.name in PUBLIC_RULE_STORES or store.name in EXPECTED_UNREAD:
            continue
        if store.populated or store.statements:
            problems.append(f"{store.name}: a store outside the public set was read")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that this repository's public rule sources parse."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="checkout to read (default: this repository)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print("public rule sources check did not run: root is not a directory")
        return 2
    try:
        stores = measure_public_stores(root)
    except Exception as exc:  # the reader is the instrument; its failure is not a pass
        print(
            f"public rule sources check did not run: reader raised {type(exc).__name__}"
        )
        return 2
    by_name = {store.name: store for store in stores}
    for name in PUBLIC_RULE_STORES:
        store = by_name.get(name)
        if store is not None:
            print(
                f"  {name}: {store.populated} file(s), "
                f"{store.statements} rule statement(s)"
            )
    problems = problems_in(stores)
    if problems:
        for problem in problems:
            print(f"::error::public rule source {problem}")
        print(f"public rule sources FAILED ({len(problems)} problem(s))")
        return 1
    print("public rule sources parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
