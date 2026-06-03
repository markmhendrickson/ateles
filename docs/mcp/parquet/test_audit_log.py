#!/usr/bin/env python3
"""
Test script for audit log functionality.
Run after MCP server restart to verify audit log implementation.
"""

from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
AUDIT_LOG_PATH = PROJECT_ROOT / "data" / "logs" / "audit_log.parquet"


def test_audit_log_exists():
    """Test 1: Verify audit log file exists"""
    print("Test 1: Checking if audit log exists...")
    if AUDIT_LOG_PATH.exists():
        print(f"✅ Audit log exists at {AUDIT_LOG_PATH}")
        return True
    else:
        print(f"❌ Audit log not found at {AUDIT_LOG_PATH}")
        print(
            "   This is expected if no write operations have occurred since server restart."
        )
        return False


def test_audit_log_content():
    """Test 2: Read and analyze audit log content"""
    if not AUDIT_LOG_PATH.exists():
        print("\nTest 2: Skipped (no audit log exists yet)")
        return False

    print("\nTest 2: Analyzing audit log content...")
    try:
        import pandas as pd

        df = pd.read_parquet(AUDIT_LOG_PATH)

        print("✅ Successfully read audit log")
        print(f"   Total entries: {len(df)}")
        print(f"   Columns: {list(df.columns)}")

        if len(df) > 0:
            print("\n   Operation breakdown:")
            print(df["operation"].value_counts().to_string())

            print("\n   Data types modified:")
            print(df["data_type"].value_counts().to_string())

            print("\n   Most recent operation:")
            latest = df.iloc[-1]
            print(f"   - Audit ID: {latest['audit_id']}")
            print(f"   - Timestamp: {latest['timestamp']}")
            print(f"   - Operation: {latest['operation']}")
            print(f"   - Data type: {latest['data_type']}")
            print(f"   - Record ID: {latest['record_id']}")

        return True
    except Exception as e:
        print(f"❌ Error reading audit log: {e}")
        return False


def test_schema_validation():
    """Test 3: Verify audit log matches schema"""
    if not AUDIT_LOG_PATH.exists():
        print("\nTest 3: Skipped (no audit log exists yet)")
        return False

    print("\nTest 3: Validating audit log schema...")
    try:
        import pandas as pd

        # Load audit log
        df = pd.read_parquet(AUDIT_LOG_PATH)

        # Expected columns from schema
        expected_columns = {
            "audit_id",
            "timestamp",
            "operation",
            "data_type",
            "record_id",
            "affected_fields",
            "old_values",
            "new_values",
            "user",
            "snapshot_reference",
            "notes",
        }

        actual_columns = set(df.columns)

        missing = expected_columns - actual_columns
        extra = actual_columns - expected_columns

        if not missing and not extra:
            print("✅ Schema validation passed")
            return True
        else:
            if missing:
                print(f"❌ Missing columns: {missing}")
            if extra:
                print(f"⚠️  Extra columns: {extra}")
            return False
    except Exception as e:
        print(f"❌ Error validating schema: {e}")
        return False


def print_usage_instructions():
    """Print instructions for manual testing via MCP"""
    print("\n" + "=" * 70)
    print("MANUAL TESTING INSTRUCTIONS")
    print("=" * 70)
    print(
        """
To test the audit log functionality via MCP tools:

1. Add a test record:
   mcp_parquet_add_record(
       data_type="beliefs",
       record={
           "belief_id": "test-audit-123",
           "name": "Test Record",
           "categories": "Testing",
           "confidence_level": "High",
           "date": "2025-12-17",
           "notes": "Testing audit log",
           "import_date": "2025-12-17",
           "import_source_file": "audit_test"
       }
   )

   Expected: Returns audit_id, no snapshot unless configured

2. Read audit log:
   mcp_parquet_read_audit_log(limit=10)

   Expected: Shows the add operation with new_values

3. Update the record:
   mcp_parquet_update_records(
       data_type="beliefs",
       filters={"belief_id": "test-audit-123"},
       updates={"notes": "Updated via audit test"}
   )

   Expected: Returns audit_ids, captures old and new values

4. Read audit log again:
   mcp_parquet_read_audit_log(
       data_type="beliefs",
       record_id="test-audit-123"
   )

   Expected: Shows both add and update operations

5. Rollback the update:
   mcp_parquet_rollback_operation(audit_id="<audit_id_from_update>")

   Expected: Restores original notes value, creates rollback audit entry

6. Delete the record:
   mcp_parquet_delete_records(
       data_type="beliefs",
       filters={"belief_id": "test-audit-123"}
   )

   Expected: Creates audit entry with complete record data

7. Rollback the delete:
   mcp_parquet_rollback_operation(audit_id="<audit_id_from_delete>")

   Expected: Restores the deleted record

8. Final cleanup - delete again:
   mcp_parquet_delete_records(
       data_type="beliefs",
       filters={"belief_id": "test-audit-123"}
   )
"""
    )


def main():
    print("=" * 70)
    print("AUDIT LOG IMPLEMENTATION TEST")
    print("=" * 70)
    print("\nThis script validates the audit log implementation.")
    print("Run AFTER restarting the MCP server.\n")

    results = []

    # Run tests
    results.append(("Audit log exists", test_audit_log_exists()))
    results.append(("Audit log content", test_audit_log_content()))
    results.append(("Schema validation", test_schema_validation()))

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")

    print(f"\nPassed: {passed}/{total}")

    if passed == 0:
        print("\n⚠️  No tests passed. This is expected if:")
        print("   1. MCP server hasn't been restarted yet")
        print("   2. No write operations have occurred since restart")
        print("\n   Next step: Restart MCP server and perform a write operation")
    elif passed < total:
        print("\n⚠️  Some tests failed. Review errors above.")
    else:
        print("\n✅ All tests passed! Audit log implementation is working.")

    # Print manual testing instructions
    print_usage_instructions()


if __name__ == "__main__":
    main()
