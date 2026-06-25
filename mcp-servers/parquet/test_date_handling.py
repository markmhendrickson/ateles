#!/usr/bin/env python3
"""Test date handling with both date and date_string types."""

import tempfile
from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def test_date_type():
    """Test that 'date' type stores as date objects."""
    # Create test data with date objects
    df = pd.DataFrame(
        [
            {"id": "1", "created_at": date(2024, 1, 1)},
            {"id": "2", "created_at": date(2024, 1, 2)},
        ]
    )

    # Write with explicit date schema
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        schema = pa.schema(
            [pa.field("id", pa.string()), pa.field("created_at", pa.date32())]
        )
        table = pa.Table.from_pandas(
            df, schema=schema, preserve_index=False, safe=False
        )
        pq.write_table(table, f.name)

        # Read back
        df_read = pd.read_parquet(f.name)

    # Verify type
    assert (
        df_read["created_at"].dtype == "object"
    ), f"Expected object dtype, got {df_read['created_at'].dtype}"
    assert isinstance(
        df_read["created_at"].iloc[0], date
    ), f"Expected date object, got {type(df_read['created_at'].iloc[0])}"
    print("✓ date type test passed")


def test_date_string_type():
    """Test that 'date_string' type stores as strings."""
    df = pd.DataFrame(
        [
            {"id": "1", "created_at": "2024-01-01"},
            {"id": "2", "created_at": "2024-01-02"},
        ]
    )

    # Write with explicit string schema
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        schema = pa.schema(
            [pa.field("id", pa.string()), pa.field("created_at", pa.string())]
        )
        table = pa.Table.from_pandas(
            df, schema=schema, preserve_index=False, safe=False
        )
        pq.write_table(table, f.name)

        # Read back
        df_read = pd.read_parquet(f.name)

    # Verify type
    assert (
        df_read["created_at"].dtype == "object"
    ), f"Expected object dtype, got {df_read['created_at'].dtype}"
    assert isinstance(
        df_read["created_at"].iloc[0], str
    ), f"Expected string, got {type(df_read['created_at'].iloc[0])}"
    assert (
        df_read["created_at"].iloc[0] == "2024-01-01"
    ), f"Expected '2024-01-01', got {df_read['created_at'].iloc[0]}"
    print("✓ date_string type test passed")


def test_mixed_conversion():
    """Test conversion from date objects to date_string format."""
    # Start with date objects
    df = pd.DataFrame(
        [
            {"id": "1", "created_at": date(2024, 1, 1)},
            {"id": "2", "created_at": date(2024, 1, 2)},
        ]
    )

    # Convert to strings
    df["created_at"] = df["created_at"].apply(
        lambda x: x.isoformat() if isinstance(x, date) else str(x)
    )
    df["created_at"] = df["created_at"].astype("object")

    # Write with string schema
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        schema = pa.schema(
            [pa.field("id", pa.string()), pa.field("created_at", pa.string())]
        )
        table = pa.Table.from_pandas(
            df, schema=schema, preserve_index=False, safe=False
        )
        pq.write_table(table, f.name)

        # Read back
        df_read = pd.read_parquet(f.name)

    # Verify all are strings
    assert all(
        isinstance(x, str) for x in df_read["created_at"] if x is not None
    ), "All values should be strings"
    print("✓ mixed conversion test passed")


if __name__ == "__main__":
    try:
        test_date_type()
        test_date_string_type()
        test_mixed_conversion()
        print("\n✅ All tests passed!")
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        exit(1)
