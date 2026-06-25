# Parquet MCP Server Tests

Comprehensive test suite for the parquet MCP server covering all 15 tools and core functionality.

## Test Structure

```
tests/
├── conftest.py                          # Pytest fixtures and configuration
├── unit/
│   ├── test_read_operations.py         # Read, list, get_schema, sort, query
│   ├── test_write_operations.py        # Add, update, upsert, delete
│   ├── test_aggregate_operations.py    # Statistics, aggregation, date range queries
│   ├── test_audit_operations.py        # Audit log, rollback
│   └── test_search_operations.py       # Semantic search, embeddings
└── integration/                         # (Future: end-to-end workflow tests)
```

## Test Coverage

### Tools Tested (15/15 - 100%)

- ✅ `list_data_types` - List available parquet files
- ✅ `get_schema` - Retrieve JSON schema
- ✅ `read_parquet` - Read with filters, sorting, pagination
- ✅ `search_parquet` - Semantic search with embeddings
- ✅ `generate_embeddings` - Generate OpenAI embeddings
- ✅ `add_record` - Add new records
- ✅ `update_records` - Update existing records
- ✅ `upsert_record` - Insert or update
- ✅ `delete_records` - Delete records
- ✅ `get_statistics` - Compute statistics
- ✅ `aggregate_parquet` - Group by and aggregate
- ✅ `query_with_date_range` - Date range queries
- ✅ `sort_parquet` - Sort results
- ✅ `read_audit_log` - Read audit history
- ✅ `rollback_operation` - Undo operations

### Test Count: 100+ tests

- **Read operations:** 30+ tests
- **Write operations:** 20+ tests
- **Aggregate operations:** 18+ tests
- **Audit operations:** 15+ tests
- **Search operations:** 17+ tests

## Running Tests

### Install Dependencies

```bash
pip install -r requirements-test.txt
```

### Run All Tests

```bash
pytest tests/
```

### Run Specific Test Suite

```bash
# Read operations
pytest tests/unit/test_read_operations.py

# Write operations
pytest tests/unit/test_write_operations.py

# Aggregate operations
pytest tests/unit/test_aggregate_operations.py

# Audit operations
pytest tests/unit/test_audit_operations.py

# Search operations
pytest tests/unit/test_search_operations.py
```

### Run with Coverage

```bash
pytest tests/ --cov=. --cov-report=term-missing --cov-report=html
```

### Run Only Unit Tests

```bash
pytest tests/ -m unit
```

### Run Only Fast Tests (Skip Slow)

```bash
pytest tests/ -m "not slow"
```

## Test Fixtures

### `test_data_dir`
- Creates temporary isolated `DATA_DIR` for each test session
- Includes required subdirectories: schemas, snapshots, logs, embeddings
- Automatically cleaned up after tests

### `sample_schema`
- Sample JSON schema with various field types
- Includes: id, name, value, category, created_date, active
- Saved to `$DATA_DIR/schemas/test_records_schema.json`

### `sample_parquet_file`
- Sample parquet file with 5 test records
- Includes varied data: different categories, values, dates, active status
- Located at `$DATA_DIR/test_records/test_records.parquet`

### `empty_parquet_file`
- Empty parquet file with schema but no records
- Used for testing edge cases
- Located at `$DATA_DIR/empty_records/empty_records.parquet`

### `reset_data_dir` (autouse)
- Automatically cleans up between tests
- Removes test parquet files, snapshots, audit logs
- Preserves schema directory

## Test Patterns

### Arrange-Act-Assert Pattern

```python
def test_example(test_data_dir: Path, sample_parquet_file: Path):
    """Test description."""
    from parquet_mcp_server import some_function_impl
    
    # Arrange: Set up test conditions
    filters = {"category": "A"}
    
    # Act: Execute the function
    result = some_function_impl("test_records", filters=filters)
    
    # Assert: Verify results
    assert result["success"] is True
    assert result["count"] == 2
```

### Error Handling Tests

```python
def test_error_case(test_data_dir: Path):
    """Test error handling."""
    from parquet_mcp_server import some_function_impl
    
    with pytest.raises(FileNotFoundError):
        some_function_impl("nonexistent")
```

### Edge Case Tests

```python
def test_edge_case(test_data_dir: Path, empty_parquet_file: Path):
    """Test edge case behavior."""
    from parquet_mcp_server import some_function_impl
    
    result = some_function_impl("empty_records")
    
    assert result["count"] == 0
    assert result["records"] == []
```

## Environment Variables

### Required

- `DATA_DIR` - Set automatically by test fixtures

### Optional

- `OPENAI_API_KEY` - Required for search/embeddings tests
- `MCP_FULL_SNAPSHOTS` - Enable full snapshots during tests
- `MCP_SNAPSHOT_FREQUENCY` - Snapshot frequency (daily, weekly, monthly, never)

## Skipping Tests

### Skip if OpenAI API Key Not Available

```python
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OpenAI API key not available"
)
def test_requires_openai(test_data_dir: Path):
    """Test that requires OpenAI."""
    ...
```

### Conditional Skips

```python
def test_conditional(test_data_dir: Path):
    """Test with conditional skip."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OpenAI API key not available")
    ...
```

## Test Data

### Sample Records

```python
{
    "id": "1",
    "name": "Alice",
    "value": 10.5,
    "category": "A",
    "created_date": "2025-01-01",
    "active": True
}
```

### Date Range

- All sample records have dates from 2025-01-01 to 2025-01-05
- Useful for testing date range queries

### Categories

- A: 2 records (Alice, Charlie)
- B: 2 records (Bob, Eve)
- C: 1 record (David)

### Active Status

- Active (True): 3 records (Alice, Bob, David)
- Inactive (False): 2 records (Charlie, Eve)

## Common Issues

### Import Error: Module Not Found

**Solution:** Ensure parent directory is in Python path (handled by `conftest.py`)

### OpenAI Tests Skipped

**Solution:** Set `OPENAI_API_KEY` environment variable

```bash
export OPENAI_API_KEY=your-api-key
pytest tests/
```

### Permission Denied on DATA_DIR

**Solution:** Tests use temporary directory, should not affect real data

### Audit Log Not Found

**Solution:** Normal - audit log is created on first write operation

## CI/CD Integration

### GitHub Actions

```yaml
- name: Run tests
  env:
    DATA_DIR: ${{ github.workspace }}/test_data
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  run: |
    pip install -r mcp/parquet/requirements-test.txt
    pytest mcp/parquet/tests/ --cov --cov-report=xml

- name: Upload coverage
  uses: codecov/codecov-action@v3
  with:
    files: ./coverage.xml
```

## Contributing

When adding new functionality:

1. **Write tests first** (TDD approach)
2. **Follow existing patterns** (arrange-act-assert)
3. **Add docstrings** to all test functions
4. **Test edge cases** (empty, missing, invalid)
5. **Test error handling** (use pytest.raises)
6. **Update this README** if adding new test categories

## Related Documentation

- `../parquet_mcp_server.py` - Server implementation
- `../README.md` - Server documentation
- `pytest.ini` - Pytest configuration
- `requirements-test.txt` - Test dependencies

## Test Maintenance

### Running Subset of Tests

```bash
# Only read tests
pytest tests/ -k "read"

# Only tests for specific tool
pytest tests/ -k "add_record"

# Only tests in specific file
pytest tests/unit/test_write_operations.py -k "upsert"
```

### Debugging Failed Tests

```bash
# Verbose output with print statements
pytest tests/ -v -s

# Stop on first failure
pytest tests/ -x

# Drop into debugger on failure
pytest tests/ --pdb
```

### Performance

```bash
# Show slowest tests
pytest tests/ --durations=10

# Run tests in parallel (if pytest-xdist installed)
pytest tests/ -n auto
```

---

**Test Suite Version:** 1.0.0 
**Last Updated:** 2025-01-24 
**Maintainer:** Repository Team
