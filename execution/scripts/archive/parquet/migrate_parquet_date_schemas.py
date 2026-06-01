#!/usr/bin/env python3
"""
One-time migration for normalizing date/time columns in parquet datasets.

For each specified data type (or all known types), this script:
  1. Creates a timestamped snapshot in data/snapshots/
  2. Normalizes date/time columns according to JSON schemas
  3. Rewrites the parquet file using a canonical pyarrow schema

Usage:
    python scripts/migrate_parquet_date_schemas.py --data-types arguments flows tasks
    python scripts/migrate_parquet_date_schemas.py --all
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from scripts.parquet_schema_definitions import get_pyarrow_schema

PROJECT_ROOT = Path(__file__).parent.parent
import sys

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import DATA_DIR

SCHEMAS_DIR = DATA_DIR / "schemas"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def list_available_data_types() -> list[str]:
    """List data/* subdirectories that contain a matching parquet file."""
    types: list[str] = []
    for subdir in DATA_DIR.iterdir():
        if not subdir.is_dir():
            continue
        if subdir.name in {"imports", "snapshots"}:
            continue
        parquet_path = subdir / f"{subdir.name}.parquet"
        if parquet_path.exists():
            types.append(subdir.name)
    return sorted(types)


def load_json_schema(data_type: str) -> dict:
    path = SCHEMAS_DIR / f"{data_type}_schema.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Schema file not found for data_type={data_type}: {path}"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_date_columns(schema: dict) -> list[str]:
    """Return list of columns whose declared type is 'date' or 'datetime'."""
    cols: list[str] = []
    for name, type_name in schema.get("schema", {}).items():
        if type_name in {"date", "datetime"}:
            cols.append(name)
    return cols


def normalize_dates(df: pd.DataFrame, schema_def: dict) -> pd.DataFrame:
    """Normalize all date/datetime columns according to JSON schema."""
    for name, type_name in schema_def.get("schema", {}).items():
        if name not in df.columns:
            continue

        if type_name == "date":
            df[name] = pd.to_datetime(df[name], errors="coerce").dt.date
        elif type_name == "datetime":
            df[name] = pd.to_datetime(df[name], errors="coerce")

    return df


def migrate_type(data_type: str) -> None:
    parquet_path = DATA_DIR / data_type / f"{data_type}.parquet"
    if not parquet_path.exists():
        print(f"[skip] {data_type}: parquet file not found at {parquet_path}")
        return

    print(f"[migrate] {data_type}")

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load schema and pyarrow schema
    json_schema = load_json_schema(data_type)
    pa_schema = get_pyarrow_schema(data_type)

    # Read existing data
    df = pd.read_parquet(parquet_path)

    # Snapshot BEFORE modifications
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"{data_type}-{ts}.parquet"
    print(f"  - writing snapshot to {snapshot_path}")
    table_snapshot = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table_snapshot, snapshot_path)

    # Normalize dates
    df = normalize_dates(df, json_schema)

    # Align pandas columns with schema ordering (add missing as None)
    ordered_cols: list[str] = list(json_schema.get("schema", {}).keys())
    for col in ordered_cols:
        if col not in df.columns:
            df[col] = None
    # Also keep any extra columns not in schema at the end
    for col in df.columns:
        if col not in ordered_cols:
            ordered_cols.append(col)
    df = df[ordered_cols]

    # Write back with explicit pyarrow schema where possible
    # For extra columns not in schema, pyarrow will infer types from pandas.
    print(f"  - rewriting parquet at {parquet_path}")
    table = pa.Table.from_pandas(df, schema=pa_schema, preserve_index=False, safe=False)
    pq.write_table(table, parquet_path)

    # Simple verification: re-read and attempt to parse date columns
    df_check = pd.read_parquet(parquet_path)
    date_cols = get_date_columns(json_schema)
    for col in date_cols:
        try:
            _ = pd.to_datetime(df_check[col], errors="raise")
        except Exception as e:
            print(
                f"  ! warning: column {col} in {data_type} still has parsing issues: {e}"
            )

    print(f"  - done {data_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate parquet date/time schemas.")
    parser.add_argument(
        "--data-types",
        nargs="*",
        help="Specific data types to migrate (e.g., arguments flows tasks).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Migrate all available data types.",
    )
    args = parser.parse_args()

    if args.all:
        data_types = list_available_data_types()
    elif args.data_types:
        data_types = args.data_types
    else:
        parser.error("Specify --all or one or more --data-types.")

    for dt in data_types:
        try:
            migrate_type(dt)
        except FileNotFoundError as e:
            print(f"[skip] {dt}: {e}")
        except Exception as e:
            print(f"[error] {dt}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
