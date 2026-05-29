# Test Setup Guide

## Current Status

**Issue:** Test Explorer shows "No tests found"

**Root Causes:**
1. pytest is not installed
2. Existing test files (`execution/scripts/test_*.py`) are scripts, not pytest tests
3. No `tests/` directory structure

## Quick Fix

### 1. Install pytest

```bash
pip install pytest pytest-cov
```

### 2. Reload VS Code

After installing pytest, reload VS Code (Cmd+Shift+P → "Developer: Reload Window")

### 3. Verify Discovery

VS Code should now discover tests in:
- `execution/scripts/test_*.py` (if they're proper pytest tests)
- Any `tests/` directory
- Files matching `*_test.py` or `test_*.py` patterns

## Converting Scripts to Pytest Tests

Current test files like `test_mytasks_section_alternative.py` are scripts, not pytest tests.

### Before (Script):
```python
def test_alternative_approaches():
    """Test alternative ways..."""
    config = AsanaConfig.from_env()
    # ... code ...
    print("Results...")

if __name__ == "__main__":
    test_alternative_approaches()
```

### After (Pytest Test):
```python
import pytest

def test_alternative_approaches():
    """Test alternative ways..."""
    config = AsanaConfig.from_env()
    # ... code ...
    assert result is not None  # Use assertions instead of print
    # pytest will capture print output automatically
```

**Key Changes:**
- Add `import pytest` (optional but recommended)
- Use `assert` statements instead of `print` for validation
- Remove `if __name__ == "__main__"` block
- pytest will run the function automatically

## Creating Test Structure

### Recommended Structure

```
personal/
├── tests/
│   ├── __init__.py
│   ├── unit/
│   │   ├── __init__.py
│   │   └── test_*.py
│   └── integration/
│       ├── __init__.py
│       └── test_*.py
└── pyproject.toml
```

### Create Test Directory

```bash
mkdir -p tests/unit tests/integration
touch tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

## Configuration

### pyproject.toml (Already Configured)

```toml
[tool.pytest.ini_options]
testpaths = [
    "tests",
    "execution/scripts",
    "scripts",
]
python_files = ["*_test.py", "test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
```

### VS Code Settings (Already Configured)

```json
{
  "python.testing.pytestEnabled": true,
  "python.testing.autoTestDiscoverOnSaveEnabled": true
}
```

## Running Tests

### In VS Code
- Use Test Explorer panel
- Click play icon next to test
- Or use CodeLens (run/debug links above test functions)

### Command Line
```bash
# Run all tests
pytest

# Run specific file
pytest execution/scripts/test_mytasks_section_alternative.py

# Run with coverage
pytest --cov=. --cov-report=term-missing

# Verbose output
pytest -v
```

## Next Steps

1. **Install pytest:** `pip install pytest pytest-cov`
2. **Reload VS Code** to enable test discovery
3. **Convert existing test scripts** to pytest format (optional)
4. **Create `tests/` directory** for new tests (recommended)

## See Also

- `/foundation-config.yaml` - Testing configuration
- `/pyproject.toml` - Pytest settings
- `/docs/linting-guide.md` - Linting setup

