# Asana MCP Server - Testing Guide

## Overview

Comprehensive test suite for the Asana MCP server covering all functionality, data permutations, and plan limitations.

## Test Structure

```
tests/
├── __init__.py
├── conftest.py                    # pytest fixtures and configuration
├── test_import.py                 # Import operation tests (18 tests)
├── test_export.py                 # Export operation tests (14 tests)
├── test_sync.py                   # Sync operation tests (14 tests)
├── test_comments.py               # Comments import tests (13 tests)
├── test_metadata.py               # Metadata import tests (13 tests)
├── test_webhooks.py               # Webhook registration tests (6 tests)
├── test_utilities.py              # Utility tool tests (8 tests)
├── test_plan_limitations.py       # Premium feature tests (12 tests)
├── test_data_permutations.py      # Data permutation tests (10 tests)
└── fixtures/
    ├── __init__.py
    ├── test_tasks.py              # Test task data generators
    └── test_workspaces.py         # Test workspace setup
```

## Running Tests

### Prerequisites

```bash
# Install test dependencies
pip install -r requirements-test.txt

# Configure test workspaces (for integration tests)
export TEST_ASANA_SOURCE_PAT="your-test-source-pat"
export TEST_ASANA_TARGET_PAT="your-test-target-pat"
export TEST_SOURCE_WORKSPACE_GID="test-source-workspace-gid"
export TEST_TARGET_WORKSPACE_GID="test-target-workspace-gid"
```

### Run All Tests

```bash
pytest
```

### Run Specific Test Categories

```bash
# Unit tests only (fast, no external dependencies)
pytest -m unit

# Integration tests (require test workspaces)
pytest -m integration

# Premium feature tests
pytest -m premium

# Exclude slow tests
pytest -m "not slow"
```

### Run Specific Test Files

```bash
# Import tests
pytest tests/test_import.py

# Export tests
pytest tests/test_export.py

# Sync tests
pytest tests/test_sync.py
```

### Coverage Reports

```bash
# Generate coverage report
pytest --cov --cov-report=html

# View coverage report
open htmlcov/index.html
```

## Test Categories

### Unit Tests (Fast)
- No external dependencies
- Use mocks for Asana API and Parquet MCP
- Test logic and error handling
- **Run time**: <1 minute

### Integration Tests (Slow)
- Require test workspaces
- Make real API calls
- Test actual data flow
- **Run time**: 5-10 minutes
- **Note**: Only run when test workspaces configured

### Premium Tests
- Test plan-specific limitations
- Document premium feature failures
- Verify graceful degradation
- **Run time**: <1 minute

## Test Coverage

### Import Operations (test_import.py)
- ✓ Basic import with minimal properties
- ✓ Full property import
- ✓ Filter permutations (only_incomplete, assignee_gid, max_tasks, include_archived)
- ✓ Merge scenarios (new creation, existing update)
- ✓ Edge cases (empty title, long title, special characters)
- ✓ Error cases (invalid workspace, invalid assignee, API failures)

### Export Operations (test_export.py)
- ✓ Basic export
- ✓ Full property export
- ✓ Filter permutations (task_ids, limit, sync_log_filter)
- ✓ Create scenarios (new task, duplicate detection, assignee assignment)
- ✓ Update scenarios (property updates, status changes)
- ✓ Attachment upload
- ✓ Error cases (invalid data, API failures, attachment failures)

### Sync Operations (test_sync.py)
- ✓ Sync scope permutations (both, source, target)
- ✓ Three-way merge (only Asana changed, only Local changed, both changed, neither changed)
- ✓ Bidirectional sync (all directions)
- ✓ Task ID filtering
- ✓ Dry run mode
- ✓ Conflict resolution
- ✓ Error cases (sync failures, merge conflicts)

### Comments Import (test_comments.py)
- ✓ No comments, single comment, multiple comments
- ✓ HTML content
- ✓ Attachments
- ✓ Different users
- ✓ Special characters, long comments, empty comments
- ✓ Error cases (invalid GIDs, API failures)

### Metadata Import (test_metadata.py)
- ✓ Metadata type permutations (custom_fields, dependencies, stories)
- ✓ Custom field types (text, number, enum, date)
- ✓ Dependencies (with/without, circular handling)
- ✓ Stories (various types)
- ✓ Error cases (invalid GIDs, API failures)

### Webhooks (test_webhooks.py)
- ✓ Workspace permutations (source, target, both)
- ✓ Project scenarios
- ✓ Error cases (invalid URL, invalid workspace)

### Utilities (test_utilities.py)
- ✓ get_asana_task (valid/invalid GIDs)
- ✓ list_asana_projects (active, archived, all)
- ✓ get_asana_workspace_info (valid/invalid)

### Plan Limitations (test_plan_limitations.py)
- ✓ Custom field creation (premium only)
- ✓ Advanced search (business only)
- ✓ Portfolio access (business only)
- ✓ Timeline features (business only)
- ✓ Graceful degradation
- ✓ Error handling (402, 403 errors)
- ✓ No crashes on premium failures

### Data Permutations (test_data_permutations.py)
- ✓ All property combinations
- ✓ Round-trip testing (import → export, export → import)
- ✓ Data integrity verification
- ✓ Edge cases (empty values, maximum values)
- ✓ Special characters preservation

## Plan Limitations

See [PLAN_LIMITATIONS.md](PLAN_LIMITATIONS.md) for detailed documentation of:
- Premium-only features
- Plan-specific limitations
- Rate limits
- Feature availability matrix
- Error codes and handling

## CI/CD Integration

GitHub Actions workflow configured in `.github/workflows/test.yml`:
- Runs unit tests on all pushes
- Runs integration tests on main branch pushes
- Uploads coverage reports to Codecov

## Troubleshooting

### Tests Fail to Import Modules
```bash
# Ensure you're in the correct directory
cd execution/mcp-servers/asana

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-test.txt
```

### Integration Tests Skipped
```bash
# Configure test workspaces
export TEST_ASANA_SOURCE_PAT="..."
export TEST_ASANA_TARGET_PAT="..."
export TEST_SOURCE_WORKSPACE_GID="..."
export TEST_TARGET_WORKSPACE_GID="..."
```

### Coverage Below Threshold
```bash
# View detailed coverage report
pytest --cov --cov-report=term-missing

# Identify uncovered lines and add tests
```

## Contributing

When adding new functionality:
1. Write tests first (TDD approach)
2. Ensure all test categories are covered
3. Update this guide if new test patterns are added
4. Maintain ≥90% coverage

## References

- [Foundation Testing Standards](../../foundation/docs/mcp-server-development-guide.md#testing--validation)
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)






