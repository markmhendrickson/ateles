#!/usr/bin/env python3
"""Copy the rule-inventory candidate corpus into a bounded data-only tree.

The canonical measurement workflow runs this script from the trusted default
branch on a hosted runner. It never executes a file from the candidate checkout:
the candidate contributes only the fixed repository inputs enumerated here.
The privileged runner validates the resulting manifest before the trusted
renderer reads the tree as data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

MANIFEST = ".canonical-rule-inventory-inputs.json"
EXACT_INPUTS = frozenset(
    {
        "CLAUDE.md",
        "docs/foundation/rule_inventory.md",
        "lib/daemon_runtime/agent_loader.py",
    }
)
MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024


class InputBoundaryError(Exception):
    """A candidate data tree crossed the fixed measurement boundary."""


def _allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = path.parts
    return (
        relative in EXACT_INPUTS
        or (
            len(parts) == 4
            and parts[:2] == (".claude", "skills")
            and parts[3] == "SKILL.md"
        )
        or (
            len(parts) == 3
            and parts[:2] == (".claude", "hooks")
            and path.suffix == ".py"
        )
    )


def _candidate_paths(source: Path) -> list[Path]:
    paths = [source / relative for relative in sorted(EXACT_INPUTS)]
    skills = source / ".claude" / "skills"
    hooks = source / ".claude" / "hooks"
    if skills.is_dir() and not skills.is_symlink():
        paths.extend(sorted(skills.glob("*/SKILL.md")))
    if hooks.is_dir() and not hooks.is_symlink():
        paths.extend(sorted(hooks.glob("*.py")))
    return paths


def _read_regular_file(root: Path, path: Path) -> tuple[str, bytes]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise InputBoundaryError from exc
    if not _allowed(relative) or path.is_symlink() or not path.is_file():
        raise InputBoundaryError
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InputBoundaryError from exc
    if not resolved.is_relative_to(root):
        raise InputBoundaryError
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise InputBoundaryError
    return relative, data


def package_inputs(source: Path, destination: Path) -> None:
    if (
        not source.is_absolute()
        or source.is_symlink()
        or not source.is_dir()
        or destination.exists()
    ):
        raise InputBoundaryError
    source = source.resolve(strict=True)
    records: list[dict[str, object]] = []
    total = 0
    for path in _candidate_paths(source):
        relative, data = _read_regular_file(source, path)
        total += len(data)
        if len(records) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise InputBoundaryError
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    if not EXACT_INPUTS.issubset({record["path"] for record in records}):
        raise InputBoundaryError
    manifest = {"schema": 1, "files": records}
    (destination / MANIFEST).write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def validate_inputs(root: Path) -> None:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise InputBoundaryError
    root = root.resolve(strict=True)
    manifest_path = root / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InputBoundaryError
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputBoundaryError from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise InputBoundaryError
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) > MAX_FILES:
        raise InputBoundaryError
    expected: set[str] = set()
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise InputBoundaryError
        relative = record["path"]
        digest = record["sha256"]
        size = record["size"]
        if (
            not isinstance(relative, str)
            or relative in expected
            or not _allowed(relative)
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(size, int)
            or size < 0
            or size > MAX_FILE_BYTES
        ):
            raise InputBoundaryError
        candidate = root / relative
        _, data = _read_regular_file(root, candidate)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise InputBoundaryError
        expected.add(relative)
        total += size
    if total > MAX_TOTAL_BYTES or not EXACT_INPUTS.issubset(expected):
        raise InputBoundaryError
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InputBoundaryError
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected | {MANIFEST}:
        raise InputBoundaryError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    try:
        if args.validate is not None:
            if args.source is not None or args.destination is not None:
                raise InputBoundaryError
            validate_inputs(args.validate)
        else:
            if args.source is None or args.destination is None:
                raise InputBoundaryError
            package_inputs(args.source, args.destination)
    except (InputBoundaryError, OSError, UnicodeError):
        print("canonical rule inventory input boundary rejected", file=sys.stderr)
        return 2
    print("canonical rule inventory input boundary verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
