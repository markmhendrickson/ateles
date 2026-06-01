# MCP Server Testing Guide

## Purpose

Provide a single reference for creating and maintaining tests for MCP servers in line with cursor rules (plan execution testing, mcp server testing, comprehensive test coverage).

## Scope

Covers Python and TypeScript MCP servers under `mcp/`, test layout, fixtures, and CI. Does not cover non-MCP application tests.

## Overview

All MCP servers must have comprehensive test coverage per `.cursor/rules/plan_execution_testing.mdc` and `.cursor/rules/mcp_server_testing.mdc`.

**Test Requirements:**
- Tests must contain actual test logic (not skeleton/TODO comments)
- Cover unit tests, integration tests, error cases, edge cases
- Tests must be runnable with proper imports and mocks
- Follow existing test patterns in codebase

## Test Structure

### Python MCP Servers

```
mcp/{server}/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Pytest fixtures
│   ├── unit/                    # Unit tests
│   │   ├── __init__.py
│   │   └── test_*.py
│   └── integration/             # Integration tests
│       ├── __init__.py
│       └── test_*.py
├── pytest.ini                   # Pytest configuration
└── requirements-test.txt        # Test dependencies
```

### TypeScript MCP Servers

```
mcp/{server}/
├── tests/
│   ├── unit/
│   │   └── *.test.ts
│   └── integration/
│       └── *.test.ts
├── vitest.config.ts             # Vitest configuration
└── package.json                 # Include test scripts
```

## Python Test Setup

### pytest.ini

```ini
[pytest]
python_files = test_*.py
python_classes = Test*
python_functions = test_*
testpaths = tests
markers =
    unit: Unit tests
    integration: Integration tests
addopts = -v --strict-markers --tb=short
minversion = 3.9
```

### requirements-test.txt

```
pytest>=7.4.0
pytest-cov>=4.1.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.0
responses>=0.22.0  # HTTP mocking
```

### conftest.py Template

```python
"""
Pytest configuration for {server} MCP server tests.
"""
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

@pytest.fixture
def mock_client():
    """Mock API client."""
    return Mock()

@pytest.fixture
def sample_data():
    """Sample test data."""
    return {}
```

## TypeScript Test Setup

### vitest.config.ts

```typescript
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    testTimeout: 30000,
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
    },
  },
})
```

### package.json Test Scripts

```json
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest",
    "test:coverage": "vitest run --coverage"
  },
  "devDependencies": {
    "@vitest/coverage-v8": "^3.1.1",
    "vitest": "^3.1.1"
  }
}
```

## Test Examples

### Python Unit Test

```python
"""
Unit tests for {feature} operations.
"""
import pytest
from unittest.mock import Mock, patch

class TestFeature:
    """Tests for feature functionality."""

    def test_success_case(self, mock_client):
        """Test successful operation."""
        # Arrange
        mock_client.method.return_value = {"success": True}

        # Act
        # result = function_under_test(mock_client)

        # Assert
        # assert result["success"] is True
        pass

    def test_error_handling(self, mock_client):
        """Test error handling."""
        mock_client.method.side_effect = Exception("API Error")

        with pytest.raises(Exception):
            # function_under_test(mock_client)
            pass
```

### TypeScript Unit Test

```typescript
import { describe, it, expect, vi } from 'vitest'

describe('Feature', () => {
  it('should handle success case', async () => {
    // Arrange
    const mockClient = {
      method: vi.fn().mockResolvedValue({ success: true })
    }

    // Act
    // const result = await functionUnderTest(mockClient)

    // Assert
    // expect(result.success).toBe(true)
  })

  it('should handle errors', async () => {
    const mockClient = {
      method: vi.fn().mockRejectedValue(new Error('API Error'))
    }

    // await expect(functionUnderTest(mockClient)).rejects.toThrow('API Error')
  })
})
```

## Running Tests

### Python Servers

```bash
# Install dependencies
pip install -r mcp/{server}/requirements-test.txt

# Run all tests
pytest mcp/{server}/tests/ -v

# Run with coverage
pytest mcp/{server}/tests/ --cov=mcp/{server} --cov-report=html

# Run only unit tests
pytest mcp/{server}/tests/ -m unit
```

### TypeScript Servers

```bash
# Install dependencies
cd mcp/{server}
npm install

# Run tests
npm test

# Run with coverage
npm run test:coverage

# Watch mode
npm run test:watch
```

## Test Patterns from Existing Servers

### Reference Implementations

**Best Examples:**
- **Google Calendar** (`mcp/google-calendar/`): 663 passing tests with vitest
- **Asana** (`mcp/asana/`): 9 test files with comprehensive fixtures
- **Parquet** (`mcp/parquet/`): Utility function tests with isolated test data

**Key Patterns:**
1. **Mock external APIs** - Never hit real APIs in tests
2. **Use fixtures** - Create reusable test data and mocks
3. **Test edge cases** - Empty data, missing fields, errors
4. **Isolated state** - Each test should be independent
5. **Clear naming** - Test names should describe what is being tested

## Common Issues

### Import Errors

**Problem:** `ModuleNotFoundError` when running tests

**Solution:** Add parent directory to sys.path in conftest.py:
```python
sys.path.insert(0, str(Path(__file__).parent.parent))
```

### Missing Dependencies

**Problem:** Tests fail due to missing test dependencies

**Solution:** Install test dependencies:
```bash
pip install -r requirements-test.txt  # Python
npm install                           # TypeScript
```

### API Credentials

**Problem:** Tests require API credentials

**Solution:** Mock API calls instead of using real credentials
```python
with patch('module.api_call') as mock_call:
    mock_call.return_value = {"data": "mocked"}
    # run test
```

## Coverage Goals

**Target Coverage per Server:**
- Unit test coverage: >80%
- Critical path coverage: 100%
- Error handling coverage: >70%

**Running Coverage Reports:**
```bash
# Python
pytest tests/ --cov --cov-report=html
open htmlcov/index.html

# TypeScript
npm run test:coverage
open coverage/index.html
```

## Continuous Integration

Tests run automatically on:
- Push to main/develop branches
- Pull requests
- Changes to MCP server code

See [`.github/workflows/test-mcp-servers.yml`](../../.github/workflows/test-mcp-servers.yml) for CI configuration.

## Adding Tests for New Servers

When creating a new MCP server:

1. **Create test structure** - Use templates above
2. **Add pytest.ini or vitest.config.ts** - Configure test runner
3. **Create conftest.py** - Add fixtures
4. **Write unit tests** - Test individual functions
5. **Write integration tests** - Test end-to-end workflows
6. **Add to CI** - Update workflow files to include new server

## Test Maintenance

**When modifying MCP server code:**
1. Run tests before committing: `pytest tests/` or `npm test`
2. Fix any failing tests
3. Add tests for new functionality
4. Update fixtures if data structures change

**Per `.cursor/rules/mcp_server_testing.mdc`:**
- Always test after code changes
- Restart server in Cursor if tests pass
- Fix issues before proceeding

## Related Documentation

- [`.cursor/rules/plan_execution_testing.mdc`](../../.cursor/rules/plan_execution_testing.mdc)
- [`.cursor/rules/mcp_server_testing.mdc`](../../.cursor/rules/mcp_server_testing.mdc)
- [`reports/test-coverage-analysis-2025-01-24.md`](../../reports/test-coverage-analysis-2025-01-24.md)
