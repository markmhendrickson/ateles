#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from parquet_to_neotoma_migration import (
    build_migration_matrix,
    ensure_parent_directory,
    matrix_to_json_rows,
    render_matrix_markdown,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_JSON_OUTPUT = (
    REPO_ROOT / "data" / "tmp" / "parquet_to_neotoma_migration_matrix.json"
)
DEFAULT_MD_OUTPUT = (
    REPO_ROOT
    / "docs"
    / "private"
    / "neotoma"
    / "parquet_to_neotoma_migration_matrix.md"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the Parquet to Neotoma migration matrix."
    )
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--md-output", type=Path, default=DEFAULT_MD_OUTPUT)
    args = parser.parse_args()

    matrix = build_migration_matrix()

    ensure_parent_directory(args.json_output)
    args.json_output.write_text(
        json.dumps(matrix_to_json_rows(matrix), indent=2), encoding="utf-8"
    )

    ensure_parent_directory(args.md_output)
    args.md_output.write_text(render_matrix_markdown(matrix), encoding="utf-8")

    print(f"Wrote {len(matrix)} matrix entries to {args.json_output}")
    print(f"Wrote markdown matrix to {args.md_output}")


if __name__ == "__main__":
    main()
