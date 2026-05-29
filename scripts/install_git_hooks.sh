#!/bin/bash
# Install git hooks for automatic submodule initialization
# This script copies hooks from scripts/git-hooks/ to .git/hooks/

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOOKS_DIR="scripts/git-hooks"
GIT_HOOKS_DIR=".git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "Error: $HOOKS_DIR not found"
    exit 1
fi

if [ ! -d "$GIT_HOOKS_DIR" ]; then
    echo "Error: $GIT_HOOKS_DIR not found (not a git repository?)"
    exit 1
fi

echo "Installing git hooks..."

for hook in "$HOOKS_DIR"/*; do
    if [ -f "$hook" ] && [ -x "$hook" ]; then
        hook_name=$(basename "$hook")
        target="$GIT_HOOKS_DIR/$hook_name"
        
        # Backup existing hook if it exists and is not our hook
        if [ -f "$target" ] && ! grep -q "automatically initialize submodules" "$target" 2>/dev/null; then
            echo "  Backing up existing $hook_name to ${hook_name}.backup"
            cp "$target" "${target}.backup"
        fi
        
        cp "$hook" "$target"
        chmod +x "$target"
        echo "  ✓ Installed $hook_name"
    fi
done

echo ""
echo "✓ Git hooks installed successfully"
echo ""
echo "Hooks will automatically:"
echo "  - Initialize submodules (post-checkout, post-merge)"
echo "  - Regenerate MCP config when template changes (post-checkout, post-merge, post-commit)"
echo ""
echo "Triggers:"
echo "  - Cloning the repository (post-checkout)"
echo "  - Checking out branches (post-checkout)"
echo "  - Pulling/merging updates (post-merge)"
echo "  - Committing template changes (post-commit)"
echo ""
echo "Manual commands:"
echo "  ./scripts/init_submodules.sh          # Initialize submodules"
echo "  ./scripts/generate_mcp_config.py      # Regenerate MCP config"
