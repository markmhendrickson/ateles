#!/usr/bin/env python3
"""
Write and verify every version line a Neotoma release candidate must carry.

## Why this exists

`npm version <x> --no-git-tag-version` writes package.json and
package-lock.json only. The product repository also pins its version in two
prose lines that its unit suite checks against package.json
(`tests/unit/llms_txt_claims.test.ts`):

- README.md: `Neotoma v<x> provides <n> MCP tools and <m> REST paths in its
  OpenAPI spec.`
- docs/known_limitations.md: ``Applies to `neotoma@<x>`.``

SECURITY.md's supported-versions table (the `X.Y.x` "Yes" row and the `< X.Y`
row) must move with each release too; no product test reads it.

Release candidates that bumped package.json alone failed the product
repository's main check (neotoma#2945, neotoma#2947). The product repository
has no script that writes these lines, so this step writes them
deterministically and then verifies all of them before the candidate is opened.

## Usage

    python3 version_lines.py --repo <neotoma checkout> --version v0.32.0
    python3 version_lines.py --repo <neotoma checkout> --version v0.32.0 --check
    python3 version_lines.py --repo <neotoma checkout> --version v0.32.0 --product-check

Without `--check`, the prose lines are rewritten in place. Every mode then
verifies package.json, package-lock.json and both prose lines. With
`--product-check`, the product repository's own test
(`tests/unit/llms_txt_claims.test.ts`) also runs. Exits 0 only when every
check passes; otherwise prints what is missing and exits 1, and the caller
must not open the candidate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_SEMVER = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"
_MINOR = r"\d+\.\d+"

# (path relative to the product repo, pattern with the version in group 2).
# Each pattern must match exactly once; a missing or repeated line is a stop,
# not a silent skip, because the product check will fail on it.
PROSE_VERSION_LINES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "README.md",
        re.compile(
            rf"^(Neotoma v)({_SEMVER})"
            r"( provides \d+ MCP tools and \d+ REST paths in its OpenAPI spec\.)",
            re.M,
        ),
    ),
    (
        "docs/known_limitations.md",
        re.compile(rf"^(Applies to `neotoma@)({_SEMVER})(`\.)", re.M),
    ),
    # SECURITY.md's supported-versions table names the newest minor line twice.
    # No product test reads it, so only this step keeps it current.
    (
        "SECURITY.md",
        re.compile(rf"^(\| `)({_MINOR})(\.x`[ \t]*\| Yes[ \t]*\|)", re.M),
    ),
    (
        "SECURITY.md",
        re.compile(rf"^(\| `< )({_MINOR})(`)", re.M),
    ),
)

# Files whose line states the minor line (X.Y), not the full version.
_MINOR_FILES = frozenset({"SECURITY.md"})


def _wanted(rel: str, full: str) -> str:
    return ".".join(full.split(".")[:2]) if rel in _MINOR_FILES else full

PRODUCT_CHECK_TEST = "tests/unit/llms_txt_claims.test.ts"
PRODUCT_CHECK_TIMEOUT_S = 300


class VersionLineError(Exception):
    """A required version line is absent, repeated or unreadable."""


def _bare(version: str) -> str:
    v = version.strip()
    v = v[1:] if v.startswith("v") else v
    if not re.fullmatch(_SEMVER, v):
        raise VersionLineError(f"not a semver version: {version!r}")
    return v


def _find_one(repo: Path, rel: str, pattern: re.Pattern[str]) -> tuple[str, re.Match[str]]:
    path = repo / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionLineError(f"{rel}: cannot read ({exc})") from exc
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise VersionLineError(
            f"{rel}: expected exactly one version line matching "
            f"{pattern.pattern!r}, found {len(matches)}"
        )
    return text, matches[0]


def write_prose_lines(repo: Path, version: str) -> list[str]:
    """Rewrite every prose version line to `version`. Returns files changed.

    Every line is located before any file is written, so a missing line leaves
    the tree untouched.
    """
    want = _bare(version)
    for rel, pat in PROSE_VERSION_LINES:
        _find_one(repo, rel, pat)
    changed: list[str] = []
    for rel, pat in PROSE_VERSION_LINES:
        # Re-read per line: one file may carry more than one version line.
        text, m = _find_one(repo, rel, pat)
        value = _wanted(rel, want)
        if m.group(2) == value:
            continue
        new = text[: m.start(2)] + value + text[m.end(2) :]
        (repo / rel).write_text(new, encoding="utf-8")
        if rel not in changed:
            changed.append(rel)
    return changed


def verify(repo: Path, version: str) -> list[str]:
    """Return one problem per version line that does not state `version`."""
    want = _bare(version)
    problems: list[str] = []
    try:
        pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
        if pkg.get("version") != want:
            problems.append(f"package.json: version is {pkg.get('version')!r}, want {want!r}")
    except (OSError, ValueError) as exc:
        problems.append(f"package.json: unreadable ({exc})")
    try:
        lock = json.loads((repo / "package-lock.json").read_text(encoding="utf-8"))
        if lock.get("version") != want:
            problems.append(
                f"package-lock.json: version is {lock.get('version')!r}, want {want!r}"
            )
        root_pkg = (lock.get("packages") or {}).get("") or {}
        if root_pkg.get("version") != want:
            problems.append(
                'package-lock.json: packages[""].version is '
                f"{root_pkg.get('version')!r}, want {want!r}"
            )
    except (OSError, ValueError) as exc:
        problems.append(f"package-lock.json: unreadable ({exc})")
    for rel, pat in PROSE_VERSION_LINES:
        try:
            _, m = _find_one(repo, rel, pat)
        except VersionLineError as exc:
            problems.append(str(exc))
            continue
        value = _wanted(rel, want)
        if m.group(2) != value:
            problems.append(f"{rel}: states {m.group(2)!r}, want {value!r}")
    return problems


def run_product_check(repo: Path) -> tuple[bool, str]:
    """Run the product repository's own version-line test with its own vitest."""
    vitest = repo / "node_modules" / ".bin" / "vitest"
    if not vitest.exists():
        return False, f"{vitest} not found: run `npm ci` in {repo} first"
    if not (repo / PRODUCT_CHECK_TEST).exists():
        return False, f"{PRODUCT_CHECK_TEST} not found in {repo}"
    try:
        proc = subprocess.run(
            [str(vitest), "run", PRODUCT_CHECK_TEST],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=PRODUCT_CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"product check did not complete: {type(exc).__name__}: {exc}"
    tail = (proc.stdout + proc.stderr)[-4000:]
    return proc.returncode == 0, tail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="Neotoma checkout (the release worktree)")
    ap.add_argument("--version", required=True, help="release tag, e.g. v0.32.0")
    ap.add_argument("--check", action="store_true", help="verify only, write nothing")
    ap.add_argument(
        "--product-check",
        action="store_true",
        help=f"also run the product repository's {PRODUCT_CHECK_TEST}",
    )
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()

    try:
        if not args.check:
            for rel in write_prose_lines(repo, args.version):
                print(f"wrote {rel}")
        problems = verify(repo, args.version)
    except VersionLineError as exc:
        print(f"VERSION LINES INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("VERSION LINES INCOMPLETE — do not open the candidate:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"all version lines state {_bare(args.version)}")

    if args.product_check:
        ok, output = run_product_check(repo)
        if not ok:
            print(
                f"PRODUCT CHECK FAILED ({PRODUCT_CHECK_TEST}) — do not open the "
                f"candidate:\n{output}",
                file=sys.stderr,
            )
            return 1
        print(f"product check passed: {PRODUCT_CHECK_TEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
