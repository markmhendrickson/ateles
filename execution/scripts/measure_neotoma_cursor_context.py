#!/usr/bin/env python3
"""Print byte and estimated-token sizes for Neotoma-related Cursor context.

Estimates tokens as bytes/4 (~English technical prose). Actual model tokenization varies.

Usage:
  python3 execution/scripts/measure_neotoma_cursor_context.py
  python3 execution/scripts/measure_neotoma_cursor_context.py --with-mcp-path /path/to/user-neotoma/INSTRUCTIONS.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_mcp_instructions_candidates() -> list[Path]:
    home = Path.home()
    return [
        home
        / ".cursor"
        / "projects"
        / "Users-markmhendrickson-repos-ateles"
        / "mcps"
        / "user-neotoma"
        / "INSTRUCTIONS.md",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-mcp-path",
        type=Path,
        help="Explicit path to user-neotoma INSTRUCTIONS.md (Cursor project folder)",
    )
    args = parser.parse_args()

    root = repo_root()
    rules = root / ".cursor" / "rules"

    paths: list[tuple[str, Path]] = []

    for pattern in (
        "neotoma_harness.mdc",
        "neotoma_evaluator_storage.mdc",
        "neotoma_qa_reflection_deep.mdc",
        "neotoma_cli.mdc",
        "mcp_auth_prompt.mdc",
    ):
        p = rules / pattern
        if p.exists():
            paths.append((pattern, p))

    mcp_path = args.with_mcp_path
    if not mcp_path:
        for c in default_mcp_instructions_candidates():
            if c.exists():
                mcp_path = c
                break

    if mcp_path and mcp_path.exists():
        paths.append(("user-neotoma INSTRUCTIONS.md (optional)", mcp_path))

    if not paths:
        print("No Neotoma rule files found under .cursor/rules/", file=sys.stderr)
        return 1

    print("Neotoma-related Cursor context sizes\n")
    total_b = 0
    for label, p in sorted(paths, key=lambda x: x[1].as_posix()):
        b = p.stat().st_size
        total_b += b
        approx = max(1, b // 4)
        print(f"  {approx:6d}  ~tokens   {b:7d} B   {p}")

    print(
        f"\n  {max(1, total_b // 4):6d}  ~tokens   {total_b:7d} B   TOTAL (listed files)"
    )

    cli = rules / "neotoma_cli.mdc"
    if not cli.exists():
        print("\nNote: neotoma_cli.mdc not present in this repo.")
        print(
            "  It may be installed by `neotoma cli-instructions check` (~/.cursor/rules/ or project)."
        )

    if not mcp_path or not mcp_path.exists():
        print(
            "\nNote: user-neotoma INSTRUCTIONS.md not found. Pass --with-mcp-path if you use Neotoma MCP in Cursor."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
