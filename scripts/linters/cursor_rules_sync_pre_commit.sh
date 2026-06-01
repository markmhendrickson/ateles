#!/bin/bash
# Cursor rules sync pre-commit hook
# Ensures .cursor/rules/ and .cursor/skills/ are synced when sources change

set -e

# Rule and skill source patterns
RULE_SOURCES="docs/.*\.mdc|foundation/agent_instructions/cursor_rules/.*\.mdc"
SKILL_SOURCES="foundation/agent_instructions/cursor_skills/.*/SKILL\.md"

# Get staged files
STAGED_FILES=$(git diff --cached --name-only)

if [ -z "$STAGED_FILES" ]; then
    exit 0
fi

# Check if any rule or skill sources are in the staged set
RULE_CHANGES=$(echo "$STAGED_FILES" | grep -E "$RULE_SOURCES" || true)
SKILL_CHANGES=$(echo "$STAGED_FILES" | grep -E "$SKILL_SOURCES" || true)

if [ -n "$RULE_CHANGES" ] || [ -n "$SKILL_CHANGES" ]; then
    echo "✓ Rule/skill source changes detected, running setup_cursor_copies..." >&2
    
    # Find the setup script
    if [ -f "foundation/scripts/setup_cursor_copies.sh" ]; then
        SETUP_SCRIPT="foundation/scripts/setup_cursor_copies.sh"
    elif [ -f "scripts/setup_cursor_copies.sh" ]; then
        SETUP_SCRIPT="scripts/setup_cursor_copies.sh"
    else
        echo "ERROR: setup_cursor_copies.sh not found" >&2
        echo "Run manually: /setup_cursor_copies or ./foundation/scripts/setup_cursor_copies.sh" >&2
        exit 1
    fi
    
    # Run the sync script
    if ! bash "$SETUP_SCRIPT"; then
        echo "ERROR: setup_cursor_copies failed" >&2
        exit 1
    fi
    
    # Check if .cursor/ files changed
    CURSOR_CHANGES=$(git status --porcelain .cursor/rules/ .cursor/skills/ 2>/dev/null | grep -E "^ M|^M|^\?\?" || true)
    
    if [ -n "$CURSOR_CHANGES" ]; then
        echo "" >&2
        echo "✓ .cursor/ files updated. Staging changes..." >&2
        git add .cursor/rules/ .cursor/skills/ 2>/dev/null || true
        echo "✓ Synced .cursor files added to commit" >&2
    else
        echo "✓ .cursor/ files already up to date" >&2
    fi
fi

exit 0
