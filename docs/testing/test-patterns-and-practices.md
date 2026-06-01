# Test Patterns and Best Practices

## Purpose

Document shared patterns and practices for writing and organizing tests so all test code meets repo standards (full implementation, coverage, isolation).

## Scope

Applies to unit, integration, and e2e tests across the repo. Does not replace tool-specific guides (e.g. MCP server testing guide).

## Core Principles

1. **Full Implementation** - No skeleton tests with TODO comments
2. **Runnable Tests** - All tests must execute successfully
3. **Comprehensive Coverage** - Unit, integration, error cases, edge cases
4. **Clear Intent** - Test names describe what is being tested
5. **Isolated State** - Tests don't depend on each other

## Test Organization

### By Type

**Unit Tests:**
- Test individual functions in isolation
- Mock all external dependencies
- Fast execution (< 1 second per test)
- No file I/O, network calls, or database access

**Integration Tests:**
- Test complete workflows end-to-end
- May use real dependencies (files, databases)
- Slower execution (acceptable)
- Clean up state after each test

**End-to-End Tests:**
- Test user-facing functionality
- Use real or staging environments
- Slowest execution
- Run less frequently (e.g., before releases)

### By Component

```
tests/
├── unit/
│   ├── feature_a/
│   │   ├── test_component1.py
│   │   └── test_component2.py
│   └── feature_b/
│       └── test_component3.py
├── integration/
│   ├── test_workflow_a.py
│   └── test_workflow_b.py
└── e2e/
    └── test_user_journey.py
```

## Naming Conventions

### Test Files

- Python: `test_{feature}.py` or `test_{component}.py`
- TypeScript: `{feature}.test.ts` or `{component}.test.ts`
- Place in directory matching component being tested

### Test Functions/Methods

```python
# Good
def test_create_user_with_valid_data():
def test_create_user_raises_error_on_duplicate_email():
def test_delete_user_returns_true_when_user_exists():

# Bad (too vague)
def test_create():
def test_user():
def test_1():
```

### Test Classes

```python
class TestUserCreation:
    def test_with_valid_data(self):
    def test_with_missing_email(self):
    def test_with_duplicate_email(self):

class TestUserDeletion:
    def test_existing_user(self):
    def test_nonexistent_user(self):
```

## Arrange-Act-Assert Pattern

```python
def test_example():
    """Test description."""
    # Arrange: Set up test conditions
    user_data = {"name": "Test", "email": "test@example.com"}
    mock_db = Mock()

    # Act: Execute the function under test
    result = create_user(mock_db, user_data)

    # Assert: Verify the results
    assert result["success"] is True
    assert mock_db.insert.called
```

## Mocking Strategies

### Mock External API Calls

```python
from unittest.mock import Mock, patch

def test_api_call():
    """Test function that calls external API."""
    with patch('module.requests.get') as mock_get:
        mock_get.return_value.json.return_value = {"data": "test"}
        result = function_that_calls_api()
        assert result == {"data": "test"}
```

### Mock Dependencies

```python
@pytest.fixture
def mock_gmail_client():
    """Mock Gmail API client."""
    client = Mock()
    client.users.messages.list.return_value = {"messages": []}
    return client

def test_with_mock(mock_gmail_client):
    """Test using mocked client."""
    result = fetch_emails(mock_gmail_client)
    assert isinstance(result, list)
```

### Mock Async Functions

```python
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_async_function():
    """Test async function."""
    mock_client = Mock()
    mock_client.fetch = AsyncMock(return_value={"data": "test"})

    result = await async_function(mock_client)
    assert result["data"] == "test"
```

## Fixtures

### Reusable Test Data

```python
@pytest.fixture
def sample_user():
    """Sample user data for testing."""
    return {
        "id": "123",
        "name": "Test User",
        "email": "test@example.com"
    }

@pytest.fixture
def sample_users():
    """Multiple sample users."""
    return [
        {"id": "1", "name": "Alice"},
        {"id": "2", "name": "Bob"},
        {"id": "3", "name": "Charlie"},
    ]
```

### Setup and Teardown

```python
@pytest.fixture
def temp_database(tmp_path):
    """Create temporary database for testing."""
    db_path = tmp_path / "test.db"
    # Create database
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()
```

### Isolated Test Environment

```python
@pytest.fixture(scope="session")
def test_data_dir():
    """Create isolated test data directory."""
    import tempfile
    import shutil

    temp_dir = Path(tempfile.mkdtemp())
    original_data_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(temp_dir)

    yield temp_dir

    # Cleanup
    if original_data_dir:
        os.environ["DATA_DIR"] = original_data_dir
    shutil.rmtree(temp_dir, ignore_errors=True)
```

## Error Testing

### Testing Expected Errors

```python
def test_raises_value_error():
    """Test that function raises ValueError for invalid input."""
    with pytest.raises(ValueError):
        function_with_validation(invalid_data)

def test_raises_with_message():
    """Test error message content."""
    with pytest.raises(ValueError, match="Invalid email"):
        function_with_validation({"email": "invalid"})
```

### Testing Error Handling

```python
def test_handles_api_error_gracefully():
    """Test graceful handling of API errors."""
    mock_client = Mock()
    mock_client.fetch.side_effect = APIError("Service unavailable")

    result = function_with_error_handling(mock_client)

    assert result["success"] is False
    assert "error" in result
```

## Edge Cases

### Common Edge Cases to Test

1. **Empty Collections**
   ```python
   def test_empty_list(self):
       result = process_items([])
       assert result == []
   ```

2. **None/Null Values**
   ```python
   def test_none_value(self):
       result = process_value(None)
       assert result is None or result == default_value
   ```

3. **Large Datasets**
   ```python
   def test_large_dataset(self):
       large_data = [{"id": i} for i in range(10000)]
       result = process_batch(large_data)
       assert len(result) == 10000
   ```

4. **Boundary Values**
   ```python
   def test_min_value(self):
       assert validate_age(0) is True

   def test_max_value(self):
       assert validate_age(120) is True
   ```

5. **Special Characters**
   ```python
   def test_special_characters(self):
       result = process_text("Test @#$ %^& *()[]")
       assert result is not None
   ```

## Integration Testing

### Testing Workflows

```python
@pytest.mark.integration
def test_complete_workflow(test_data_dir):
    """Test complete user workflow."""
    # 1. Create resource
    user = create_user({"name": "Test"})

    # 2. Modify resource
    update_user(user["id"], {"name": "Updated"})

    # 3. Retrieve resource
    retrieved = get_user(user["id"])
    assert retrieved["name"] == "Updated"

    # 4. Delete resource
    delete_user(user["id"])
    assert get_user(user["id"]) is None
```

### Testing with Real Files

```python
@pytest.mark.integration
def test_file_operations(tmp_path):
    """Test operations with real files."""
    test_file = tmp_path / "test.csv"

    # Write
    write_csv(test_file, data)
    assert test_file.exists()

    # Read
    result = read_csv(test_file)
    assert result == data

    # Cleanup is automatic (tmp_path is removed after test)
```

## Performance Testing

### Testing Response Time

```python
import time

def test_performance():
    """Test that operation completes within time limit."""
    start = time.time()
    result = expensive_operation()
    duration = time.time() - start

    assert duration < 1.0  # Should complete within 1 second
    assert result is not None
```

### Mark Slow Tests

```python
@pytest.mark.slow
def test_large_import():
    """Test importing large dataset."""
    # This test takes a long time
    pass

# Skip slow tests in regular runs
# pytest tests/ -m "not slow"
```

## Test Data Management

### Using Faker

```python
from faker import Faker

@pytest.fixture
def faker_instance():
    return Faker()

def test_with_fake_data(faker_instance):
    """Test using generated fake data."""
    email = faker_instance.email()
    name = faker_instance.name()

    user = create_user({"email": email, "name": name})
    assert user["email"] == email
```

### Parametrized Tests

```python
@pytest.mark.parametrize("input,expected", [
    ("alice@example.com", True),
    ("invalid-email", False),
    ("", False),
    (None, False),
])
def test_email_validation(input, expected):
    """Test email validation with various inputs."""
    assert validate_email(input) == expected
```

## Test Coverage

### Measuring Coverage

```bash
# Python
pytest tests/ --cov=. --cov-report=term-missing

# TypeScript
npm run test:coverage
```

### Coverage Goals

- **Overall**: >80%
- **Critical paths**: 100%
- **Error handling**: >70%
- **Edge cases**: >60%

### Excluding from Coverage

```python
# pytest.ini
[pytest]
[tool:pytest]
coverage_exclude_lines =
    pragma: no cover
    def __repr__
    raise AssertionError
    raise NotImplementedError
    if __name__ == .__main__.:
    if TYPE_CHECKING:
```

## Debugging Tests

### Run Single Test

```bash
# Python
pytest tests/unit/test_file.py::TestClass::test_method -v

# TypeScript
npm test -- tests/unit/file.test.ts
```

### Print Debug Output

```python
def test_with_debugging(capfd):
    """Test with captured output."""
    print("Debug info")
    result = function()

    captured = capfd.readouterr()
    assert "Debug info" in captured.out
```

### Use Debugger

```bash
# Python: Drop into debugger on failure
pytest tests/ --pdb

# Python: Drop into debugger on first failure
pytest tests/ -x --pdb
```

## Reference Implementations

**Study these servers for test patterns:**

1. **mcp/google-calendar/** - TypeScript, vitest, 663 tests
2. **mcp/asana/** - Python, pytest, comprehensive fixtures
3. **mcp/parquet/** - Python, pytest, isolated test data

## Checklist for New Tests

- [ ] Test file follows naming convention (`test_*.py` or `*.test.ts`)
- [ ] Tests are in appropriate directory (`unit/` or `integration/`)
- [ ] All tests have clear docstrings/descriptions
- [ ] External dependencies are mocked
- [ ] Tests cover success cases
- [ ] Tests cover error cases
- [ ] Tests cover edge cases (empty, null, boundary values)
- [ ] Tests are independent (no shared state)
- [ ] Tests clean up after themselves
- [ ] Tests run successfully in CI
- [ ] Coverage meets minimum threshold (80%)

## Related Documentation

- [MCP Server Testing Guide](./mcp-server-test-guide.md)
- [Test Coverage Analysis](../../reports/test-coverage-analysis-2025-01-24.md)
- [`.cursor/rules/plan_execution_testing.mdc`](../../.cursor/rules/plan_execution_testing.mdc)
- [`.cursor/rules/mcp_server_testing.mdc`](../../.cursor/rules/mcp_server_testing.mdc)
