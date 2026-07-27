#!/bin/bash
# Run all linters on the codebase
# Usage: ./scripts/lint.sh [--fix]

set -e

FIX_FLAG=""
if [ "$1" == "--fix" ]; then
    FIX_FLAG="--fix"
fi

echo "🔍 Running all linters..."
echo ""

# Check if pre-commit is available
if command -v pre-commit &> /dev/null; then
    echo "Using pre-commit to run all linters..."
    if [ -n "$FIX_FLAG" ]; then
        pre-commit run --all-files
    else
        pre-commit run --all-files
    fi
    exit $?
fi

# Fallback: run linters individually
echo "Pre-commit not installed. Running linters individually..."
echo "Install with: pip install pre-commit && pre-commit install"
echo ""

ERRORS=0

# Python linting with Ruff
echo "📝 Running Ruff..."
if [ -n "$FIX_FLAG" ]; then
    ruff check . --fix || ERRORS=$((ERRORS + 1))
    ruff format . || ERRORS=$((ERRORS + 1))
else
    ruff check . || ERRORS=$((ERRORS + 1))
    ruff format --check . || ERRORS=$((ERRORS + 1))
fi

# YAML linting
echo "📝 Running yamllint..."
yamllint . || ERRORS=$((ERRORS + 1))

# Shell script linting
echo "📝 Running ShellCheck..."
find . -name "*.sh" -not -path "./.venv/*" -not -path "./venv*/*" -exec shellcheck {} \; || ERRORS=$((ERRORS + 1))

# Custom linters
echo "📝 Running custom linters..."

# Parquet access linter
echo "  - Checking parquet access patterns..."
find . -name "*.py" -not -path "./.venv/*" -not -path "./venv*/*" -not -path "./mcp-servers/parquet/*" -not -path "./execution/scripts/*_import.py" | \
    xargs python scripts/linters/ast_parquet_linter.py || ERRORS=$((ERRORS + 1))

# File naming
echo "  - Checking file naming conventions..."
find . -type f \( -name "*.py" -o -name "*.sh" -o -name "*.md" \) -not -path "./.venv/*" -not -path "./venv*/*" | \
    xargs python scripts/linters/check_file_naming.py || ERRORS=$((ERRORS + 1))

# Documentation
echo "  - Checking documentation structure..."
find . -name "*.md" -path "./docs/*" -o -name "*.md" -path "./strategy/*" | \
    xargs python scripts/linters/check_documentation.py || ERRORS=$((ERRORS + 1))

# Workflow compliance
echo "  - Checking workflow file compliance..."
find ./strategy/operations -name "*.md" | \
    xargs python scripts/linters/check_workflow_compliance.py || ERRORS=$((ERRORS + 1))

# Config sourcing (operator-specific config must be env/Neotoma-sourced)
echo "  - Checking config sourcing (no hardcoded operator config)..."
python scripts/linters/check_hardcoded_config.py || ERRORS=$((ERRORS + 1))

# tool_allowlist grant grammar (ateles#255 — bash: prefix is silently dropped
# by the CLI's --allowedTools parser; only Bash(<command>:*) is honored).
# Requires live Neotoma; skipped (not failed) when NEOTOMA_BASE_URL is unset
# so this stays runnable on a machine without Neotoma access.
if [ -n "$NEOTOMA_BASE_URL" ]; then
    echo "  - Checking agent_definition tool_allowlist grant grammar..."
    python scripts/linters/validate_tool_allowlist.py || ERRORS=$((ERRORS + 1))

    # Generated agent-doc mirrors must be fresh (docs/agents/*.md,
    # .claude/skills/*/SKILL.md are rendered FROM Neotoma agent_definition
    # entities — never hand-edited). Informational only for now: pre-existing
    # drift unrelated to any one PR currently fails this check repo-wide (see
    # .github/workflows/agent-config-validation.yml for the tracking note).
    echo "  - Checking agent-doc mirrors are in sync with Neotoma (informational)..."
    python execution/scripts/render_agent_docs.py --check || true
else
    echo "  - Skipping tool_allowlist + agent-doc-mirror checks (NEOTOMA_BASE_URL unset)"
fi

echo ""
if [ $ERRORS -eq 0 ]; then
    echo "✅ All linters passed!"
    exit 0
else
    echo "❌ Found $ERRORS linter error(s)"
    echo "Run with --fix to auto-fix some issues: ./scripts/lint.sh --fix"
    exit 1
fi

