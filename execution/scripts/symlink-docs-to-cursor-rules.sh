#!/bin/bash
# Symlink Docs to Cursor Rules
#
# Purpose:
#   Wrapper script that calls foundation/scripts/setup_cursor_rules.sh
#   to create symlinks for repository rules (unprefixed).
#
#   This makes agent rule files (files with "_rules" suffix) available as Cursor rules
#   that are automatically loaded into context for AI agents.
#
# Usage:
#   ./execution/scripts/symlink-docs-to-cursor-rules.sh
#
#   Or use directly:
#   ./foundation/scripts/setup_cursor_rules.sh
#
# Behavior:
#   - Calls foundation/scripts/setup_cursor_rules.sh (no REPO_RULES_PREFIX)
#   - Creates symlinks from docs/ directory to .cursor/rules/ directory
#   - Only symlinks files with "_rules" suffix (e.g., "communication_rules.md")
#   - Uses original filenames (no prefix) to match foundation rules behavior
#
# Notes:
#   - Cursor automatically loads all .md files from .cursor/rules/ into context
#   - Symlinks ensure docs/ remains the single source of truth
#   - Changes to files in docs/ are immediately reflected in .cursor/rules/
#   - Symlink names use original filenames (e.g., "communication_rules.mdc")
#   - Only agent rule files (with "_rules" suffix) are symlinked, not all documentation
#
# Related:
#   - foundation/scripts/setup_cursor_rules.sh - Main symlink script (handles foundation and repo rules)
#   - docs/decision_framework_rules.md - Decision-making framework

set -e

# Find repository root
REPO_ROOT=""
if [ -d "docs" ]; then
    REPO_ROOT="."
elif [ -d "../docs" ]; then
    REPO_ROOT=".."
else
    echo "Error: docs/ directory not found. Please run from repository root or parent directory."
    exit 1
fi

# Change to repo root
cd "$REPO_ROOT" || exit 1

# Check if foundation script exists
if [ ! -f "foundation/scripts/setup_cursor_rules.sh" ]; then
    echo "Error: foundation/scripts/setup_cursor_rules.sh not found."
    echo "Please ensure foundation submodule is initialized."
    exit 1
fi

# Run foundation script without repo rules prefix (unprefixed)
bash foundation/scripts/setup_cursor_rules.sh
