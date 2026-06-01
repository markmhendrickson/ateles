#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from parquet_to_neotoma_migration import build_migration_matrix, ensure_parent_directory

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT = (
    REPO_ROOT / "docs" / "private" / "neotoma" / "parquet_to_neotoma_wave_report.md"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a per-wave migration report from the live matrix."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    matrix = build_migration_matrix()
    by_wave = defaultdict(list)
    for entry in matrix:
        by_wave[entry.wave].append(entry)

    lines = ["# Parquet to Neotoma Wave Report", ""]
    for wave in ("pilot", "wave_a", "wave_b", "wave_c"):
        entries = sorted(
            by_wave[wave], key=lambda item: (-item.row_count, item.data_type)
        )
        row_total = sum(entry.row_count for entry in entries)
        lines.append(f"## {wave}")
        lines.append("")
        lines.append(f"- Data types: {len(entries)}")
        lines.append(f"- Rows: {row_total}")
        lines.append("")
        lines.append(
            "| Data type | Rows | Target entity type | Primary key | Strategy |"
        )
        lines.append("| --- | ---: | --- | --- | --- |")
        for entry in entries:
            lines.append(
                f"| {entry.data_type} | {entry.row_count} | {entry.target_entity_type} | {entry.primary_key or ''} | {entry.strategy} |"
            )
        lines.append("")

    ensure_parent_directory(args.output)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote wave report to {args.output}")


if __name__ == "__main__":
    main()
