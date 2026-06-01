#!/bin/bash
# Push nested repositories to forks
# Run this after creating the forks on GitHub

set -e

REPO_DIR="/Users/markmhendrickson/Projects/personal"

echo "Pushing nested repositories to forks..."
echo ""

cd "$REPO_DIR/mcp-servers/gmail"
echo "=== Pushing mcp-server-gmail ==="
if git push -u origin main 2>&1; then
    echo "✓ mcp-server-gmail pushed successfully"
else
    echo "✗ mcp-server-gmail push failed - fork may not exist yet"
    echo "  Expected: https://github.com/markmhendrickson/mcp-server-gmail"
fi
echo ""

cd "$REPO_DIR/mcp-servers/google-calendar"
echo "=== Pushing mcp-server-google-calendar ==="
if git push -u origin main 2>&1; then
    echo "✓ mcp-server-google-calendar pushed successfully"
else
    echo "✗ mcp-server-google-calendar push failed - fork may not exist yet"
    echo "  Expected: https://github.com/markmhendrickson/mcp-server-google-calendar"
fi
echo ""

cd "$REPO_DIR/mcp-servers/instagram"
echo "=== Pushing mcp-server-instagram ==="
if git push -u origin main 2>&1; then
    echo "✓ mcp-server-instagram pushed successfully"
else
    echo "✗ mcp-server-instagram push failed - fork may not exist yet"
    echo "  Expected: https://github.com/markmhendrickson/mcp-server-instagram"
fi
echo ""

echo "Done!"

