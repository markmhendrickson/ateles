# Custom Linters

This directory contains custom linters that enforce foundation agent instructions beyond what standard tools can check.

## Linters

### `ast_parquet_linter.py`

AST-based linter that detects direct parquet file access violations.

**Purpose:** Enforces MCP-only data access policy by detecting direct `pd.read_parquet()` or `df.to_parquet()` calls.

**Usage:**
```bash
python scripts/linters/ast_parquet_linter.py [file1.py] [file2.py] ...
```

**Exceptions:**
- Files in `mcp-servers/parquet/` (MCP server implementation)
- Import scripts (`*_import.py`)
- Troubleshooting scripts (`troubleshoot_*.py`)

### `check_file_naming.py`

Enforces file naming conventions from `foundation-config.yaml`.

**Purpose:** Validates that files follow naming patterns:
- Python: `snake_case.py`
- Shell: `kebab-case.sh`
- Reports: `*-report-YYYY-MM-DD.md`

**Usage:**
```bash
python scripts/linters/check_file_naming.py [file1] [file2] ...
```

### `check_documentation.py`

Validates documentation structure and required sections.

**Purpose:** Checks markdown files for:
- Required sections (Purpose, Scope)
- Link validity (basic checks)

**Usage:**
```bash
python scripts/linters/check_documentation.py [file1.md] [file2.md] ...
```

### `check_workflow_compliance.py`

Validates workflow files are in correct locations.

**Purpose:** Ensures report files are in the correct directories per workflow requirements.

**Usage:**
```bash
python scripts/linters/check_workflow_compliance.py [file1.md] [file2.md] ...
```

### `check_hardcoded_config.py`

Enforces the Ateles "Neotoma-canonical / env-sourced" config rule in daemon and
runtime code.

**Purpose:** Flags operator-specific config (operator/personal email literals,
Google Calendar resource IDs, IBANs, Bitcoin addresses) hardcoded in `lib/`,
`execution/daemons/`, and `execution/scripts/` Python — including in docstring
examples, since this is a public repo. Per `docs/architecture.md` and `CLAUDE.md`,
these must be read from env / parquet / Neotoma at runtime — never baked in so
the swarm stays portable and operator-agnostic.

**Not redundant with the gitleaks PII scan:** `.gitleaks.toml` guards against
*third-party* PII leaking into the public repo, so it deliberately allowlists
the operator's own email and calendar IDs. This linter enforces the *separate*
sourcing rule that gitleaks does not.

**Usage:**
```bash
python scripts/linters/check_hardcoded_config.py [file1.py ...]   # or no args → scans runtime dirs
```

**Suppression:** append `# config-source-ok: <reason>` to a line with a
reviewed, intentional literal (e.g. an explicit env-default fallback).

### `check_protected_paths.sh`

Prevents commits to protected directories.

**Purpose:** Blocks commits to:
- `$DATA_DIR/imports/` (read-only archive)
- `$DATA_DIR/attachments/` (large files)
- `docs/private/` (private submodule)
- `.env*` files

**Usage:**
```bash
bash scripts/linters/check_protected_paths.sh
```

## Integration

All linters are integrated into `.pre-commit-config.yaml` and run automatically on commit.

## Development

To add a new linter:

1. Create the linter script in this directory
2. Make it executable: `chmod +x scripts/linters/new_linter.py`
3. Add to `.pre-commit-config.yaml`:
```yaml
- repo: local
  hooks:
    - id: new-linter
      name: Description of what it checks
      entry: python scripts/linters/new_linter.py
      language: system
      types: [python]  # or other file types
```

## Testing

Test linters individually:
```bash
# Test parquet linter
python scripts/linters/ast_parquet_linter.py path/to/test.py

# Test file naming
python scripts/linters/check_file_naming.py path/to/test.py

# Test documentation
python scripts/linters/check_documentation.py path/to/test.md
```

## See Also

- `/docs/linting-guide.md` - Complete linting guide
- `/reports/foundation-agent-instructions-linter-analysis.md` - Analysis of what can be linted
- `/.pre-commit-config.yaml` - Pre-commit configuration

