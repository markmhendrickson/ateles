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
BOUNDARY_REASONS = frozenset(
    {
        "destination_exists",
        "invalid_root",
        "malformed_manifest",
        "missing_exact_inputs",
        "oversized",
        "path_escape",
        "symlink",
        "undeclared_path",
    }
)


class InputBoundaryError(Exception):
    """A candidate data tree crossed the fixed measurement boundary."""

    def __init__(self, reason: str) -> None:
        if reason not in BOUNDARY_REASONS:
            raise ValueError("unknown canonical input-boundary reason")
        self.reason = reason
        super().__init__(reason)


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
    claude = source / ".claude"
    if claude.is_symlink():
        raise InputBoundaryError("symlink")
    if claude.exists() and not claude.is_dir():
        raise InputBoundaryError("invalid_root")
    for store, pattern in (
        (claude / "skills", "*/SKILL.md"),
        (claude / "hooks", "*.py"),
    ):
        if store.is_symlink():
            raise InputBoundaryError("symlink")
        if store.exists() and not store.is_dir():
            raise InputBoundaryError("invalid_root")
        if store.is_dir():
            paths.extend(sorted(store.glob(pattern)))
    return paths


def _read_regular_file(root: Path, path: Path) -> tuple[str, bytes]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise InputBoundaryError("path_escape") from exc
    if path.is_symlink():
        raise InputBoundaryError("symlink")
    if not _allowed(relative):
        raise InputBoundaryError("undeclared_path")
    if not path.is_file():
        raise InputBoundaryError("missing_exact_inputs")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InputBoundaryError("missing_exact_inputs") from exc
    if not resolved.is_relative_to(root):
        raise InputBoundaryError("path_escape")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InputBoundaryError("missing_exact_inputs") from exc
    if len(data) > MAX_FILE_BYTES:
        raise InputBoundaryError("oversized")
    return relative, data


def package_inputs(source: Path, destination: Path) -> None:
    if not source.is_absolute() or not source.is_dir():
        raise InputBoundaryError("invalid_root")
    if source.is_symlink():
        raise InputBoundaryError("symlink")
    if destination.exists():
        raise InputBoundaryError("destination_exists")
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise InputBoundaryError("invalid_root") from exc
    records: list[dict[str, object]] = []
    total = 0
    for path in _candidate_paths(source):
        relative, data = _read_regular_file(source, path)
        total += len(data)
        if len(records) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise InputBoundaryError("oversized")
        target = destination / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise InputBoundaryError("invalid_root") from exc
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    if not EXACT_INPUTS.issubset({record["path"] for record in records}):
        raise InputBoundaryError("missing_exact_inputs")
    manifest = {"schema": 1, "files": records}
    try:
        (destination / MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise InputBoundaryError("invalid_root") from exc


def validate_inputs(root: Path) -> None:
    if not root.is_absolute() or not root.is_dir():
        raise InputBoundaryError("invalid_root")
    if root.is_symlink():
        raise InputBoundaryError("symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise InputBoundaryError("invalid_root") from exc
    manifest_path = root / MANIFEST
    if manifest_path.is_symlink():
        raise InputBoundaryError("symlink")
    if not manifest_path.is_file():
        raise InputBoundaryError("malformed_manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputBoundaryError("malformed_manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise InputBoundaryError("malformed_manifest")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) > MAX_FILES:
        reason = "oversized" if isinstance(records, list) else "malformed_manifest"
        raise InputBoundaryError(reason)
    expected: set[str] = set()
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise InputBoundaryError("malformed_manifest")
        relative = record["path"]
        digest = record["sha256"]
        size = record["size"]
        if not isinstance(relative, str) or relative in expected:
            raise InputBoundaryError("malformed_manifest")
        if not _allowed(relative):
            raise InputBoundaryError("undeclared_path")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(size, int)
            or size < 0
        ):
            raise InputBoundaryError("malformed_manifest")
        if size > MAX_FILE_BYTES:
            raise InputBoundaryError("oversized")
        candidate = root / relative
        _, data = _read_regular_file(root, candidate)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise InputBoundaryError("malformed_manifest")
        expected.add(relative)
        total += size
    if total > MAX_TOTAL_BYTES:
        raise InputBoundaryError("oversized")
    if not EXACT_INPUTS.issubset(expected):
        raise InputBoundaryError("missing_exact_inputs")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InputBoundaryError("symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    declared = expected | {MANIFEST}
    if actual - declared:
        raise InputBoundaryError("undeclared_path")
    if declared - actual:
        raise InputBoundaryError("missing_exact_inputs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    try:
        if args.validate is not None:
            if args.source is not None or args.destination is not None:
                raise InputBoundaryError("invalid_root")
            validate_inputs(args.validate)
        else:
            if args.source is None or args.destination is None:
                raise InputBoundaryError("invalid_root")
            package_inputs(args.source, args.destination)
    except InputBoundaryError as exc:
        print(
            f"canonical rule inventory input boundary rejected reason={exc.reason}",
            file=sys.stderr,
        )
        return 2
    print("canonical rule inventory input boundary verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
