#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from parquet_to_neotoma_migration import (
    build_migration_matrix,
    delete_parquet_rows,
    ensure_parent_directory,
    get_parquet_client,
    map_row_to_entity,
    read_all_parquet_rows,
    store_entities_batch,
    verify_migration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REPORT = REPO_ROOT / "data" / "tmp" / "parquet_to_neotoma_migration_report.json"


def selected_entries(matrix, args):
    if args.data_types:
        wanted = {part.strip() for part in args.data_types.split(",") if part.strip()}
        return [entry for entry in matrix if entry.data_type in wanted]
    if args.wave == "all":
        return matrix
    return [entry for entry in matrix if entry.wave == args.wave]


def flush_batch(batch, dry_run: bool) -> tuple[int, int, list[str]]:
    if not batch:
        return 0, 0, []
    entities = [entity for entity, _, _ in batch]
    keys = [idem for _, idem, _ in batch]
    batch_key = keys[0] if len(keys) == 1 else f"{keys[0]}__to__{keys[-1]}"
    if dry_run:
        return len(batch), 0, []
    ok, message = store_entities_batch(entities, batch_key)
    if ok:
        return len(batch), 0, []
    return 0, len(batch), [message]


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Parquet rows to Neotoma.")
    parser.add_argument(
        "--wave",
        type=str,
        default="pilot",
        choices=["pilot", "wave_a", "wave_b", "wave_c", "all"],
        help="Which migration wave to run.",
    )
    parser.add_argument(
        "--data-types",
        type=str,
        help="Comma-separated list of data types to migrate instead of wave selection.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=25, help="Entities per Neotoma store call."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build batches without writing to Neotoma.",
    )
    parser.add_argument(
        "--cleanup-parquet-after-verify",
        action="store_true",
        help="Delete successfully migrated Parquet rows after verification passes.",
    )
    parser.add_argument(
        "--limit-per-type",
        type=int,
        default=0,
        help="Limit number of source rows per data type (0 = all).",
    )
    parser.add_argument("--offset", type=int, default=0, help="Per-type source offset.")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    client = get_parquet_client()
    matrix = build_migration_matrix(client)
    entries = selected_entries(matrix, args)

    report: list[dict] = []
    for entry in entries:
        rows = read_all_parquet_rows(
            client,
            entry.data_type,
            limit=args.limit_per_type,
            offset=args.offset,
        )
        batch = []
        stored = 0
        failed = 0
        errors: list[str] = []
        for row in rows:
            entity, idem = map_row_to_entity(entry, row)
            batch.append((entity, idem, row))
            if len(batch) >= args.batch_size:
                ok_count, fail_count, batch_errors = flush_batch(batch, args.dry_run)
                stored += ok_count
                failed += fail_count
                errors.extend(batch_errors)
                batch = []
        if batch:
            ok_count, fail_count, batch_errors = flush_batch(batch, args.dry_run)
            stored += ok_count
            failed += fail_count
            errors.extend(batch_errors)

        verification = (
            {"verified": False, "reason": "dry-run"}
            if args.dry_run
            else verify_migration(entry, rows)
        )

        deleted = 0
        if (
            args.cleanup_parquet_after_verify
            and not args.dry_run
            and verification.get("verified")
        ):
            deleted = delete_parquet_rows(client, entry, rows)

        report.append(
            {
                "data_type": entry.data_type,
                "target_entity_type": entry.target_entity_type,
                "wave": entry.wave,
                "source_rows": len(rows),
                "stored": stored,
                "failed": failed,
                "deleted_from_parquet": deleted,
                "verification": verification,
                "errors": errors[:20],
            }
        )
        print(
            f"{entry.data_type}: source={len(rows)} stored={stored} failed={failed} verified={verification.get('verified')}"
        )

    ensure_parent_directory(args.report_path)
    args.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote report to {args.report_path}")


if __name__ == "__main__":
    main()
