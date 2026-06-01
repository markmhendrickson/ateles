#!/bin/bash
# Create GitHub repositories for MCP servers and push commits
# This script creates the repos (if they don't exist) and pushes all commits

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Create and Push MCP Server Repositories ==="
echo ""

# Check for GitHub CLI
if ! command -v gh &> /dev/null; then
    echo "❌ GitHub CLI not found."
    echo ""
    echo "Install it with:"
    echo "  brew install gh"
    echo "  gh auth login"
    echo ""
    echo "Or create repositories manually on GitHub, then run:"
    echo "  ./scripts/push_nested_repos.sh"
    exit 1
fi

# Check authentication
if ! gh auth status &> /dev/null; then
    echo "❌ GitHub CLI not authenticated."
    echo ""
    echo "Please run:"
    echo "  gh auth login"
    exit 1
fi

echo "Creating GitHub repositories..."
echo ""

# Create repositories (will skip if they already exist)
create_repo() {
    local repo_name=$1
    local description=$2
    
    if gh repo view "markmhendrickson/$repo_name" &> /dev/null; then
        echo "  ✓ $repo_name already exists"
    else
        echo "  Creating $repo_name..."
        gh repo create "markmhendrickson/$repo_name" \
            --public \
            --description "$description" \
            --clone=false 2>&1 | head -2
        echo "  ✓ Created $repo_name"
    fi
}

create_repo "mcp-server-gmail" "Gmail MCP Server - fork of GongRzhe/Gmail-MCP-Server"
create_repo "mcp-server-google-calendar" "Google Calendar MCP Server - fork of nspady/google-calendar-mcp"
create_repo "mcp-server-instagram" "Instagram MCP Server - fork of jlbadano/ig-mcp"
create_repo "mcp-server-parquet" "Parquet MCP Server - MCP server for interacting with parquet files"
create_repo "mcp-server-minted" "Minted MCP Server - MCP server for Minted.com API integration"

echo ""
echo "Pushing commits to repositories..."
echo ""

# Push each repository
push_repo() {
    local repo_path=$1
    local repo_name=$(basename "$repo_path")
    
    if [ ! -d "$repo_path/.git" ]; then
        echo "  ⚠️  $repo_name: Not a git repository"
        return
    fi
    
    cd "$repo_path"
    
    echo "=== Pushing $repo_name ==="
    
    # Check if there are commits to push
    if git rev-parse --verify origin/main &> /dev/null; then
        LOCAL=$(git rev-parse main)
        REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "")
        
        if [ "$LOCAL" != "$REMOTE" ]; then
            if git push -u origin main 2>&1; then
                echo "  ✓ Pushed successfully"
            else
                echo "  ✗ Push failed"
                return 1
            fi
        else
            echo "  ✓ Already up to date"
        fi
    else
        # First push
        if git push -u origin main 2>&1; then
            echo "  ✓ Pushed successfully (first push)"
        else
            echo "  ✗ Push failed"
            return 1
        fi
    fi
    
    cd "$REPO_ROOT"
    echo ""
}

push_repo "mcp-servers/gmail"
push_repo "mcp-servers/google-calendar"
push_repo "mcp-servers/instagram"
push_repo "mcp-servers/parquet"
push_repo "mcp-servers/minted"

echo "=== Complete ==="
echo ""
echo "All MCP server repositories have been created and pushed!"
echo ""
echo "Repository URLs:"
echo "  - https://github.com/markmhendrickson/mcp-server-gmail"
echo "  - https://github.com/markmhendrickson/mcp-server-google-calendar"
echo "  - https://github.com/markmhendrickson/mcp-server-instagram"
echo "  - https://github.com/markmhendrickson/mcp-server-parquet"
echo "  - https://github.com/markmhendrickson/mcp-server-minted"

