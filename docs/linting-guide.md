# Linting Guide

This repository uses automated linters to enforce foundation agent instructions and code conventions.

## Purpose

Document the repository's lint suite — what each linter checks, how to install and run them, and how to fix or
suppress findings.

## Scope

Covers the repository's linters, their pre-commit / git-hook wiring, and the policy docs they cite
(`docs/policies/`). Not a prose style guide.

## Quick Start

### Installation

1. Install pre-commit:
```bash
pip install pre-commit
```

2. Install the git hooks:
```bash
pre-commit install
```

3. (Optional) Run on all files to check current state:
```bash
pre-commit run --all-files
```

### Run All Linters

**Single command to run everything:**

```bash
# Using pre-commit (recommended)
pre-commit run --all-files

# Or using the convenience script
./scripts/lint.sh

# Auto-fix issues where possible
./scripts/lint.sh --fix
```

### Manual Linting

You can also run individual linters manually:

```bash
# Python code style
ruff check .
ruff format .

# YAML files
yamllint .

# Shell scripts
shellcheck scripts/**/*.sh

# Custom linters
python scripts/linters/ast_parquet_linter.py path/to/file.py
python scripts/linters/check_file_naming.py path/to/file.py
python scripts/linters/check_documentation.py path/to/file.md
```

## Linters Overview

### Code Conventions

**Ruff** - Python linting and formatting
- Enforces: snake_case functions/variables, PascalCase classes, UPPER_SNAKE_CASE constants
- Config: `pyproject.toml`
- Auto-fixes: Yes (with `--fix`)

**Black** - Python code formatting
- Enforces: Consistent code formatting
- Config: `pyproject.toml`
- Auto-fixes: Yes

**Yamllint** - YAML file linting
- Enforces: 2-space indentation, snake_case keys, lowercase booleans
- Config: `.yamllint.yaml`
- Auto-fixes: Limited

**ShellCheck** - Shell script linting
- Enforces: Shell script best practices
- Config: None (uses defaults)
- Auto-fixes: No

### Security

**Gitleaks** - Secrets detection
- Detects: API keys, passwords, tokens, .env files
- Config: `.gitleaks.toml`
- Auto-fixes: No (prevents commit)

**Protected Paths Checker** - Prevents commits to protected directories
- Protects: `$DATA_DIR/imports/`, `$DATA_DIR/attachments/`, `docs/private/`, `.env*`
- Config: `scripts/linters/check_protected_paths.sh`
- Auto-fixes: No (prevents commit)

### File Naming

**File Naming Checker** - Enforces naming conventions
- Enforces:
  - Python files: `snake_case.py`
  - Shell scripts: `kebab-case.sh`
  - Report files: `*-report-YYYY-MM-DD.md`
- Config: `scripts/linters/check_file_naming.py`
- Auto-fixes: No (prevents commit)

### Data Access

**Parquet Access Linter** - Enforces MCP-only data access
- Detects: Direct `pd.read_parquet()` or `df.to_parquet()` calls
- Exceptions: MCP server code, import scripts
- Config: `scripts/linters/ast_parquet_linter.py`
- Auto-fixes: No (prevents commit)
- See: `docs/policies/agent-mcp-access-policy.md`

### Documentation

**Documentation Checker** - Validates documentation structure
- Checks: Required sections (Purpose, Scope), link validity
- Config: `scripts/linters/check_documentation.py`
- Auto-fixes: No (prevents commit)

**Workflow Compliance Checker** - Validates workflow file locations
- Checks: Report files in correct directories
- Config: `scripts/linters/check_workflow_compliance.py`
- Auto-fixes: No (prevents commit)

## Configuration Files

- `pyproject.toml` - Ruff, Black, pytest configuration
- `.yamllint.yaml` - YAML linting rules
- `.gitleaks.toml` - Secrets detection patterns
- `.pre-commit-config.yaml` - Pre-commit hooks configuration

## IDE Integration (VS Code)

### Problems Tab Integration

**Will show in Problems tab:**
- ✅ **Ruff** - Python linting errors (with Ruff extension)
- ✅ **Yamllint** - YAML errors (with YAML extension)
- ✅ **ShellCheck** - Shell script errors (with ShellCheck extension)

**Won't show in Problems tab (pre-commit only):**
- ❌ **Gitleaks** - Secrets detection (runs on commit)
- ❌ **Protected paths checker** - Runs on commit
- ❌ **Custom linters** - MCP access, file naming, documentation, workflow compliance (run on commit)

### Setup

1. Install recommended VS Code extensions:
   - Open Command Palette (Cmd+Shift+P)
   - Run "Extensions: Show Recommended Extensions"
   - Install: Ruff, YAML, ShellCheck

2. VS Code will automatically use:
   - `pyproject.toml` for Ruff configuration
   - `.yamllint.yaml` for YAML linting
   - ShellCheck settings from `.vscode/settings.json`

3. Problems will appear in real-time as you type (for Ruff, YAML, ShellCheck)

### Custom Linters in IDE

To see custom linter problems in VS Code, you can:

1. **Run manually in terminal:**
   ```bash
   python scripts/linters/ast_parquet_linter.py path/to/file.py
   ```

2. **Create a VS Code task** (optional):
   - Add to `.vscode/tasks.json` to run custom linters
   - Use "Run Task" to check files

3. **Pre-commit will catch everything** - All linters run automatically on commit

## Common Issues

### "Direct parquet file access detected"

**Problem:** Code is accessing parquet files directly instead of using MCP server.

**Solution:** Use MCP tools instead:
```python
# ❌ Wrong
df = pd.read_parquet("$DATA_DIR/flows/flows.parquet")

# ✅ Correct
# Use MCP parquet server tools
```

**Exception:** If you're working on the MCP server itself (`mcp-servers/parquet/`) or import scripts, this is allowed.

### "File naming convention violation"

**Problem:** File name doesn't match required pattern.

**Solution:** Rename file to match convention:
- Python: `my_script.py` (snake_case)
- Shell: `my-script.sh` (kebab-case)
- Reports: `btc-liquidity-regime-report-2026-01-07.md`

### "Protected path violation"

**Problem:** Attempting to commit files in protected directories.

**Solution:** Remove from staging or add to `.gitignore`:
```bash
git reset HEAD path/to/protected/file
```

Protected paths:
- `$DATA_DIR/imports/` - Read-only archive
- `$DATA_DIR/attachments/` - Large files
- `docs/private/` - Private submodule
- `.env*` - Environment files

### "Missing required section"

**Problem:** Documentation file missing required sections.

**Solution:** Add required sections:
- `docs/` files: Purpose, Scope
- `strategy/` files: Purpose

## Disabling Hooks (Not Recommended)

If you need to skip hooks for a specific commit:

```bash
git commit --no-verify -m "message"
```

**Warning:** Only use this for legitimate exceptions. Most violations can be fixed automatically or with minor changes.

## Updating Linters

To update pre-commit hooks:

```bash
pre-commit autoupdate
```

To update individual tools:

```bash
pip install --upgrade ruff black yamllint shellcheck-py
```

## Test Discovery Setup

### Why Tests Don't Show

If the Test Explorer shows "No tests found", check:

1. **pytest not installed:**
   ```bash
   pip install pytest pytest-cov
   ```

2. **Test files not in expected locations:**
   - Config expects: `tests/` directory or `*_test.py` / `test_*.py` files
   - Current test files are in `execution/scripts/test_*.py`

3. **Test files not using pytest:**
   - Files need to use pytest (e.g., `import pytest`, `def test_*():`)
   - Current test files are scripts, not pytest tests

### Fixing Test Discovery

**Option 1: Install pytest and configure VS Code**
```bash
pip install pytest pytest-cov
```
Then reload VS Code - it should discover tests automatically.

**Option 2: Convert scripts to pytest tests**
- Add `import pytest` at the top
- Use pytest assertions (`assert` instead of `print`)
- Remove `if __name__ == "__main__"` blocks

**Option 3: Create proper test structure**
- Create `tests/` directory at repo root
- Add `tests/__init__.py`
- Move/convert test files to use pytest

The `pyproject.toml` is configured to discover tests in:
- `tests/` directory
- `execution/scripts/` (existing test files)
- `scripts/` directory

## Related Documentation

- `/reports/foundation-agent-instructions-linter-analysis.md` - Complete analysis
- `/foundation-config.yaml` - Foundation configuration
- `docs/policies/agent-mcp-access-policy.md` - MCP access policy
- `docs/policies/agent-workflow-requirements.md` - Workflow requirements

