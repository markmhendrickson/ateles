#!/bin/bash
# Setup MCP server forks with custom names
# This script installs GitHub CLI (if needed) and creates forks

set -e

echo "=== MCP Server Fork Setup ==="
echo ""

# Check for GitHub CLI
if ! command -v gh &> /dev/null; then
    echo "GitHub CLI not found. Installing..."
    if command -v brew &> /dev/null; then
        brew install gh
    else
        echo "❌ Homebrew not found. Please install GitHub CLI manually:"
        echo "   https://cli.github.com/"
        exit 1
    fi
fi

# Check authentication
if ! gh auth status &> /dev/null; then
    echo "GitHub CLI not authenticated. Please login:"
    gh auth login
fi

echo ""
echo "Creating forks with custom names..."
echo ""

# Create forks with custom repository names
echo "1. Creating mcp-server-gmail..."
gh repo fork GongRzhe/Gmail-MCP-Server \
    --clone=false \
    --repo markmhendrickson/mcp-server-gmail \
    || echo "⚠️  Fork may already exist or failed"

echo ""
echo "2. Creating mcp-server-google-calendar..."
gh repo fork nspady/google-calendar-mcp \
    --clone=false \
    --repo markmhendrickson/mcp-server-google-calendar \
    || echo "⚠️  Fork may already exist or failed"

echo ""
echo "3. Creating mcp-server-instagram..."
gh repo fork jlbadano/ig-mcp \
    --clone=false \
    --repo markmhendrickson/mcp-server-instagram \
    || echo "⚠️  Fork may already exist or failed"

echo ""
echo "✓ Fork creation complete!"
echo ""
echo "Next: Push your local commits:"
echo "  ./scripts/push_nested_repos.sh"

