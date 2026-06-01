"""
Define canonical pyarrow schemas for normalized parquet datasets.

These schemas are derived from JSON schema files in $DATA_DIR/schemas and used
by migration scripts (and optionally writers) to enforce stable physical
types, especially for date/time fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

PROJECT_ROOT = Path(__file__).parent.parent.parent
import sys

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import get_data_dir

SCHEMAS_DIR = get_data_dir() / "schemas"


TYPE_MAP = {
    "string": pa.string(),
    "float64": pa.float64(),
    "integer": pa.int64(),
    "int64": pa.int64(),
    "boolean": pa.bool_(),
    "bool": pa.bool_(),
    "date": pa.date32(),
    "datetime": pa.timestamp(
        "us", tz="UTC"
    ),  # Use microseconds with UTC timezone to match existing data
    "timestamp": pa.timestamp(
        "us", tz="UTC"
    ),  # Use microseconds with UTC timezone to match existing data
}


def _load_json_schema(data_type: str) -> dict:
    path = SCHEMAS_DIR / f"{data_type}_schema.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Schema file not found for data_type={data_type}: {path}"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_pyarrow_schema(data_type: str) -> pa.schema:
    """
    Build a pyarrow schema for a given data_type based on its JSON schema.

    Unknown or missing field types default to string.
    """
    raw = _load_json_schema(data_type)
    schema_def: dict[str, str] = raw.get("schema", {})

    fields = []
    for name, type_name in schema_def.items():
        pa_type = TYPE_MAP.get(type_name, TYPE_MAP["string"])
        fields.append(pa.field(name, pa_type))

    return pa.schema(fields)
