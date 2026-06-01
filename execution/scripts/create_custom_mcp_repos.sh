#!/bin/bash
# Create GitHub repositories for custom MCP servers
# Run this after initializing the git repos locally

set -e

echo "=== Creating GitHub Repositories for Custom MCP Servers ==="
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
echo "Creating repositories..."
echo ""

# Create parquet MCP server repo
echo "1. Creating mcp-server-parquet..."
gh repo create markmhendrickson/mcp-server-parquet \
    --public \
    --description "MCP server for interacting with parquet files - read, query, add, update, delete with audit trail" \
    --clone=false \
    || echo "⚠️  Repository may already exist"

echo ""

# Create minted MCP server repo
echo "2. Creating mcp-server-minted..."
gh repo create markmhendrickson/mcp-server-minted \
    --public \
    --description "MCP server for interacting with Minted.com API - address book, orders, and delivery information" \
    --clone=false \
    || echo "⚠️  Repository may already exist"

echo ""
echo "✓ Repository creation complete!"
echo ""
echo "Next: Push your local commits:"
echo "  cd mcp-servers/parquet && git push -u origin main"
echo "  cd ../minted && git push -u origin main"

