#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from parquet_to_neotoma_migration import (
    build_migration_matrix,
    ensure_parent_directory,
    get_parquet_client,
    read_all_parquet_rows,
    verify_migration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REPORT = (
    REPO_ROOT / "data" / "tmp" / "parquet_to_neotoma_verification_report.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify Parquet to Neotoma migration parity by primary key."
    )
    parser.add_argument(
        "--data-types",
        type=str,
        default="",
        help="Comma-separated data types to verify (default: all).",
    )
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    client = get_parquet_client()
    matrix = build_migration_matrix(client)
    wanted = {part.strip() for part in args.data_types.split(",") if part.strip()}
    entries = [entry for entry in matrix if not wanted or entry.data_type in wanted]

    report = []
    for entry in entries:
        rows = read_all_parquet_rows(client, entry.data_type)
        verification = verify_migration(entry, rows)
        verification["target_entity_type"] = entry.target_entity_type
        verification["wave"] = entry.wave
        report.append(verification)
        print(
            f"{entry.data_type}: verified={verification.get('verified')} missing={verification.get('missing_count', 0)}"
        )

    ensure_parent_directory(args.report_path)
    args.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote verification report to {args.report_path}")


if __name__ == "__main__":
    main()
