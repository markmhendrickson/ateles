"""
Pytest configuration and fixtures for parquet MCP server tests.
"""

import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pandas as pd
import pytest

# Add parent directory to path so we can import parquet_mcp_server
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(scope="session")
def test_data_dir() -> Generator[Path, None, None]:
    """Create a temporary data directory for tests."""
    temp_dir = Path(tempfile.mkdtemp(prefix="mcp_parquet_test_"))

    # Create required subdirectories
    (temp_dir / "schemas").mkdir(parents=True)
    (temp_dir / "snapshots").mkdir(parents=True)
    (temp_dir / "logs").mkdir(parents=True)
    (temp_dir / "embeddings").mkdir(parents=True)

    # Set environment variable for DATA_DIR
    original_data_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(temp_dir)

    yield temp_dir

    # Cleanup
    if original_data_dir:
        os.environ["DATA_DIR"] = original_data_dir
    else:
        os.environ.pop("DATA_DIR", None)

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def sample_schema(test_data_dir: Path) -> dict:
    """Create a sample schema for testing."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Test Records",
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Unique identifier"},
            "name": {"type": "string", "description": "Record name"},
            "value": {"type": "number", "description": "Numeric value"},
            "category": {
                "type": "string",
                "enum": ["A", "B", "C"],
                "description": "Category",
            },
            "created_date": {
                "type": "string",
                "format": "date",
                "description": "Creation date",
            },
            "active": {"type": "boolean", "description": "Active status"},
        },
        "required": ["id", "name"],
    }

    # Write schema to test data directory
    schema_file = test_data_dir / "schemas" / "test_records_schema.json"
    import json

    with open(schema_file, "w") as f:
        json.dump(schema, f, indent=2)

    return schema


@pytest.fixture
def sample_parquet_file(test_data_dir: Path, sample_schema: dict) -> Path:
    """Create a sample parquet file with test data."""
    data_type = "test_records"
    data_type_dir = test_data_dir / data_type
    data_type_dir.mkdir(parents=True, exist_ok=True)

    parquet_file = data_type_dir / f"{data_type}.parquet"

    # Create sample data
    df = pd.DataFrame(
        {
            "id": ["1", "2", "3", "4", "5"],
            "name": ["Alice", "Bob", "Charlie", "David", "Eve"],
            "value": [10.5, 20.3, 15.7, 30.2, 25.1],
            "category": ["A", "B", "A", "C", "B"],
            "created_date": pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"]
            ),
            "active": [True, True, False, True, False],
        }
    )

    df.to_parquet(parquet_file, index=False)

    return parquet_file


@pytest.fixture
def empty_parquet_file(test_data_dir: Path, sample_schema: dict) -> Path:
    """Create an empty parquet file for testing."""
    data_type = "empty_records"
    data_type_dir = test_data_dir / data_type
    data_type_dir.mkdir(parents=True, exist_ok=True)

    parquet_file = data_type_dir / f"{data_type}.parquet"

    # Create empty dataframe with schema columns
    df = pd.DataFrame(
        {
            "id": pd.Series(dtype="str"),
            "name": pd.Series(dtype="str"),
            "value": pd.Series(dtype="float"),
            "category": pd.Series(dtype="str"),
            "created_date": pd.Series(dtype="datetime64[ns]"),
            "active": pd.Series(dtype="bool"),
        }
    )

    df.to_parquet(parquet_file, index=False)

    return parquet_file


@pytest.fixture(autouse=True)
def reset_data_dir(test_data_dir: Path):
    """Reset data directory state between tests."""
    yield

    # Clean up parquet files (except schemas)
    for item in test_data_dir.iterdir():
        if item.name not in ["schemas", "snapshots", "logs", "embeddings"]:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink()

    # Clean up snapshots
    snapshots_dir = test_data_dir / "snapshots"
    if snapshots_dir.exists():
        for file in snapshots_dir.iterdir():
            file.unlink()

    # Clean up audit log
    audit_log = test_data_dir / "logs" / "audit_log.parquet"
    if audit_log.exists():
        audit_log.unlink()
