#!/usr/bin/env python3
"""
Audit parquet schemas and date/time column types.

Scans data/*/*.parquet (excluding data/imports and data/snapshots) and
reports, for each file:
  - pandas dtypes
  - pyarrow physical types
  - whether column name looks date/time-like

Outputs a JSON report to data/logs/parquet_schema_audit.json and prints
basic summary to stdout.
"""

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).parent.parent
import sys

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import DATA_DIR

LOGS_DIR = DATA_DIR / "logs"
OUTPUT_PATH = LOGS_DIR / "parquet_schema_audit.json"


def is_date_like(name: str) -> bool:
    """Heuristic: treat columns containing 'date' or 'time' (case-insensitive) as date/time-like."""
    lower = name.lower()
    return "date" in lower or "time" in lower


def audit_file(path: Path) -> dict:
    """Audit a single parquet file."""
    rel_path = path.relative_to(PROJECT_ROOT).as_posix()

    # Read parquet schema via pyarrow
    arrow_schema = pq.read_schema(path)
    arrow_fields = {field.name: str(field.type) for field in arrow_schema}

    # Read small sample with pandas for dtypes (avoid loading huge tables fully)
    try:
        df = pd.read_parquet(path)
        pandas_dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}
    except Exception as e:
        pandas_dtypes = {"__error__": f"{type(e).__name__}: {e}"}

    columns = {}
    for name, pa_type in arrow_fields.items():
        columns[name] = {
            "pyarrow_type": pa_type,
            "pandas_dtype": pandas_dtypes.get(name),
            "is_date_like": is_date_like(name),
        }

    return {
        "path": rel_path,
        "columns": columns,
    }


def main() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    results = []

    for subdir in DATA_DIR.iterdir():
        if not subdir.is_dir():
            continue

        # Skip imports and snapshots per policy
        if subdir.name in {"imports", "snapshots"}:
            continue

        for path in subdir.glob("*.parquet"):
            try:
                results.append(audit_file(path))
            except Exception as e:
                results.append(
                    {
                        "path": path.relative_to(PROJECT_ROOT).as_posix(),
                        "error": f"{type(e).__name__}: {e}",
                    }
                )

    # Write JSON report
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Print brief summary
    print(f"Audited {len(results)} parquet files")
    print(f"Report written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
