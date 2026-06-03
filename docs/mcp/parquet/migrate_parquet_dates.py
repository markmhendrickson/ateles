#!/usr/bin/env python3
"""
Migrate parquet files to use proper date types based on schema definitions.

Usage:
    python migrate_parquet_dates.py [--data-type TYPE] [--dry-run] [--all]
"""

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def migrate_date_columns(parquet_path: Path, schema_path: Path, dry_run: bool = False):
    """Migrate date columns in a parquet file to proper types."""
    # Load schema
    with open(schema_path) as f:
        schema_def = json.load(f).get("schema", {})

    # Read parquet file
    print(f"\nChecking {parquet_path.name}...")
    df = pd.read_parquet(parquet_path)

    # Identify columns to migrate
    migrations = []
    for col_name, type_name in schema_def.items():
        if col_name not in df.columns:
            continue

        current_dtype = df[col_name].dtype

        if type_name == "date" and current_dtype == "object":
            # Check if column contains strings (needs migration to date)
            sample = df[col_name].dropna()
            if len(sample) > 0:
                first_val = sample.iloc[0]
                if isinstance(first_val, str):
                    migrations.append((col_name, "string", "date"))
                elif isinstance(first_val, date):
                    # Already date objects, no migration needed
                    pass
        elif type_name == "date_string" and current_dtype != "object":
            migrations.append((col_name, str(current_dtype), "date_string"))

    if not migrations:
        print("  ✓ No migration needed")
        return False

    print(f"  Found {len(migrations)} column(s) to migrate:")
    for col, from_type, to_type in migrations:
        print(f"    {col}: {from_type} -> {to_type}")

    if dry_run:
        return False

    # Apply migrations
    for col_name, _, target_type in migrations:
        if target_type == "date":
            # Convert strings to date objects
            def to_date_safe(val):
                if pd.isna(val) or val is None:
                    return None
                if isinstance(val, date) and not isinstance(val, datetime):
                    return val
                if isinstance(val, datetime):
                    return val.date()
                if isinstance(val, str):
                    try:
                        dt = pd.to_datetime(val, errors="coerce")
                        if pd.isna(dt):
                            return None
                        return dt.date()
                    except:
                        return None
                return None

            df[col_name] = df[col_name].apply(to_date_safe)

        elif target_type == "date_string":
            # Convert dates to ISO format strings
            df[col_name] = df[col_name].apply(
                lambda x: (
                    x.isoformat()
                    if isinstance(x, date | datetime)
                    else str(x)
                    if x is not None and not pd.isna(x)
                    else None
                )
            )
            df[col_name] = df[col_name].astype("object")

    # Write back with proper schema
    # Build PyArrow schema
    fields = []
    for col_name in df.columns:
        if col_name in schema_def:
            type_name = schema_def[col_name]
            if type_name == "date":
                fields.append(pa.field(col_name, pa.date32()))
            elif type_name == "date_string":
                fields.append(pa.field(col_name, pa.string()))
            elif type_name == "string":
                fields.append(pa.field(col_name, pa.string()))
            elif type_name == "boolean":
                fields.append(pa.field(col_name, pa.bool_()))
            elif type_name == "integer":
                fields.append(pa.field(col_name, pa.int64()))
            elif type_name == "float":
                fields.append(pa.field(col_name, pa.float64()))
            elif type_name in {"datetime", "timestamp"}:
                fields.append(pa.field(col_name, pa.timestamp("us", tz="UTC")))
            else:
                # Unknown type, infer from DataFrame
                try:
                    fields.append(
                        pa.field(col_name, pa.from_numpy_dtype(df[col_name].dtype))
                    )
                except:
                    fields.append(pa.field(col_name, pa.string()))
        else:
            # Column not in schema, infer type
            try:
                fields.append(
                    pa.field(col_name, pa.from_numpy_dtype(df[col_name].dtype))
                )
            except:
                fields.append(pa.field(col_name, pa.string()))

    schema = pa.schema(fields)
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False, safe=False)
    pq.write_table(table, parquet_path)

    print(f"  ✓ Migrated {parquet_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate parquet date columns")
    parser.add_argument("--data-type", help="Specific data type to migrate")
    parser.add_argument("--all", action="store_true", help="Migrate all data types")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be migrated"
    )
    args = parser.parse_args()

    data_dir = Path(os.getenv("DATA_DIR", Path.home() / "data"))
    schemas_dir = data_dir / "schemas"

    print(f"Data directory: {data_dir}")
    print(f"Schemas directory: {schemas_dir}")

    # Find parquet files to migrate
    if args.data_type:
        data_types = [args.data_type]
    elif args.all:
        data_types = [
            d.name
            for d in data_dir.iterdir()
            if d.is_dir()
            and not d.name.startswith("_")
            and d.name not in {"schemas", "snapshots", "logs", "embeddings"}
        ]
    else:
        print("Error: Specify --data-type TYPE or --all")
        return 1

    print(f"\nScanning {len(data_types)} data type(s)...")

    migrated_count = 0
    for data_type in sorted(data_types):
        parquet_path = data_dir / data_type / f"{data_type}.parquet"
        schema_path = schemas_dir / f"{data_type}_schema.json"

        if not parquet_path.exists():
            continue
        if not schema_path.exists():
            print(f"\n{data_type}: No schema file found, skipping")
            continue

        try:
            did_migrate = migrate_date_columns(
                parquet_path, schema_path, dry_run=args.dry_run
            )
            if did_migrate:
                migrated_count += 1
        except Exception as e:
            print(f"  ✗ Error: {e}")

    print(f"\n{'Dry run complete' if args.dry_run else 'Migration complete'}")
    print(f"{'Would migrate' if args.dry_run else 'Migrated'} {migrated_count} file(s)")
    return 0


if __name__ == "__main__":
    exit(main())
