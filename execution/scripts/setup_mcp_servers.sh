#!/bin/bash
# Setup MCP Servers for Personal Workflow Repository
# This script initializes and installs all MCP servers

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== MCP Servers Setup ==="
echo ""

# Check if submodules exist
if [ -f ".gitmodules" ]; then
    echo "Initializing git submodules..."
    git submodule update --init --recursive
else
    echo "No submodules configured. Setting up MCP servers manually..."
    echo ""
    
    # Check if MCP server directories exist
    if [ ! -d "mcp-servers/gmail/.git" ]; then
        echo "⚠️  Gmail MCP server not found. Clone it manually:"
        echo "   git clone https://github.com/markmhendrickson/mcp-server-gmail.git mcp-servers/gmail"
    fi
    
    if [ ! -d "mcp-servers/google-calendar/.git" ]; then
        echo "⚠️  Google Calendar MCP server not found. Clone it manually:"
        echo "   git clone https://github.com/markmhendrickson/mcp-server-google-calendar.git mcp-servers/google-calendar"
    fi
    
    if [ ! -d "mcp-servers/instagram/.git" ]; then
        echo "⚠️  Instagram MCP server not found. Clone it manually:"
        echo "   git clone https://github.com/markmhendrickson/mcp-server-instagram.git mcp-servers/instagram"
    fi
fi

echo ""
echo "Installing MCP server dependencies..."
echo ""

# Parquet MCP Server (custom - always present)
if [ -d "mcp-servers/parquet" ]; then
    echo "1. Installing Parquet MCP server..."
    cd mcp-servers/parquet
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        echo "   ✓ Parquet dependencies installed"
    else
        echo "   ⚠️  requirements.txt not found"
    fi
    cd "$REPO_ROOT"
    echo ""
fi

# Minted MCP Server (custom - always present)
if [ -d "mcp-servers/minted" ]; then
    echo "2. Installing Minted MCP server..."
    cd mcp-servers/minted
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        echo "   ✓ Minted dependencies installed"
    else
        echo "   ⚠️  requirements.txt not found"
    fi
    cd "$REPO_ROOT"
    echo ""
fi

# Gmail MCP Server (fork)
if [ -d "mcp-servers/gmail" ]; then
    echo "3. Installing Gmail MCP server..."
    cd mcp-servers/gmail
    if [ -f "package.json" ]; then
        if command -v npm &> /dev/null; then
            npm install
            npm run build
            echo "   ✓ Gmail dependencies installed and built"
        else
            echo "   ⚠️  npm not found - install Node.js first"
        fi
    else
        echo "   ⚠️  package.json not found"
    fi
    cd "$REPO_ROOT"
    echo ""
fi

# Google Calendar MCP Server (fork)
if [ -d "mcp-servers/google-calendar" ]; then
    echo "4. Installing Google Calendar MCP server..."
    cd mcp-servers/google-calendar
    if [ -f "package.json" ]; then
        if command -v npm &> /dev/null; then
            npm install
            npm run build
            echo "   ✓ Google Calendar dependencies installed and built"
        else
            echo "   ⚠️  npm not found - install Node.js first"
        fi
    else
        echo "   ⚠️  package.json not found"
    fi
    cd "$REPO_ROOT"
    echo ""
fi

# Instagram MCP Server (fork)
if [ -d "mcp-servers/instagram" ]; then
    echo "5. Installing Instagram MCP server..."
    cd mcp-servers/instagram
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        echo "   ✓ Instagram dependencies installed"
    else
        echo "   ⚠️  requirements.txt not found"
    fi
    cd "$REPO_ROOT"
    echo ""
fi

echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Configure MCP servers in Cursor (see .cursor/mcp.json)"
echo "2. Or configure in Claude Desktop (see mcp-servers/README.md)"
echo "3. Restart your IDE/application to load MCP servers"
echo ""
echo "For detailed configuration, see: mcp-servers/README.md"

