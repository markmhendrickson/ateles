#!/bin/bash
# Convert existing repos to proper GitHub forks with custom names
# This deletes the current repos, creates proper forks, renames them, and pushes

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Converting to Proper Forks ==="
echo ""
echo "This will:"
echo "  1. Delete current GitHub repos"
echo "  2. Create proper forks from originals"
echo "  3. Rename forks to custom names"
echo "  4. Force push all commits"
echo ""
read -p "Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

# Gmail
echo ""
echo "1. Converting gmail repo..."
cd mcp-servers/gmail
echo "  Deleting current repo..."
gh repo delete markmhendrickson/mcp-server-gmail --yes 2>&1 | head -2
echo "  Creating fork..."
gh repo fork GongRzhe/Gmail-MCP-Server --clone=false 2>&1 | head -2
echo "  Renaming fork..."
gh repo rename mcp-server-gmail --repo markmhendrickson/Gmail-MCP-Server --yes 2>&1 | head -2
echo "  Updating remote and pushing..."
git remote set-url origin https://github.com/markmhendrickson/mcp-server-gmail.git
git push -f origin main 2>&1 | head -2
cd "$REPO_ROOT"
echo "  ✓ Gmail converted"
echo ""

# Google Calendar
echo "2. Converting google-calendar repo..."
cd mcp-servers/google-calendar
echo "  Deleting current repo..."
gh repo delete markmhendrickson/mcp-server-google-calendar --yes 2>&1 | head -2
echo "  Creating fork..."
gh repo fork nspady/google-calendar-mcp --clone=false 2>&1 | head -2
echo "  Renaming fork..."
gh repo rename mcp-server-google-calendar --repo markmhendrickson/google-calendar-mcp --yes 2>&1 | head -2
echo "  Updating remote and pushing..."
git remote set-url origin https://github.com/markmhendrickson/mcp-server-google-calendar.git
git push -f origin main 2>&1 | head -2
cd "$REPO_ROOT"
echo "  ✓ Google Calendar converted"
echo ""

# Instagram
echo "3. Converting instagram repo..."
cd mcp-servers/instagram
echo "  Deleting current repo..."
gh repo delete markmhendrickson/mcp-server-instagram --yes 2>&1 | head -2
echo "  Creating fork..."
gh repo fork jlbadano/ig-mcp --clone=false 2>&1 | head -2
echo "  Renaming fork..."
gh repo rename mcp-server-instagram --repo markmhendrickson/ig-mcp --yes 2>&1 | head -2
echo "  Updating remote and pushing..."
git remote set-url origin https://github.com/markmhendrickson/mcp-server-instagram.git
git push -f origin main 2>&1 | head -2
cd "$REPO_ROOT"
echo "  ✓ Instagram converted"
echo ""

echo "=== Complete ==="
echo ""
echo "All repos are now proper forks linked to their originals!"
echo ""
echo "Verify fork status:"
echo "  gh repo view markmhendrickson/mcp-server-gmail --json isFork,parent"
echo "  gh repo view markmhendrickson/mcp-server-google-calendar --json isFork,parent"
echo "  gh repo view markmhendrickson/mcp-server-instagram --json isFork,parent"

