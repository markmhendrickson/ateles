#!/bin/bash
# Script to set up Gmail MCP server OAuth credentials

set -e

GMAIL_SERVER_DIR="$HOME/.local/mcp-servers/mcp-gmail"
CREDENTIALS_FILE="$GMAIL_SERVER_DIR/credentials.json"
TOKEN_FILE="$GMAIL_SERVER_DIR/token.json"

echo "Gmail MCP Server OAuth Setup"
echo "=============================="
echo ""

# Check if server directory exists
if [ ! -d "$GMAIL_SERVER_DIR" ]; then
    echo "❌ Error: Gmail MCP server directory not found at: $GMAIL_SERVER_DIR"
    echo "   Please install the Gmail MCP server first."
    exit 1
fi

echo "Server directory: $GMAIL_SERVER_DIR"
echo ""

# Check for existing credentials
if [ -f "$CREDENTIALS_FILE" ]; then
    echo "✓ Found existing credentials.json"
    echo "  Location: $CREDENTIALS_FILE"
    read -p "  Do you want to replace it? (y/N): " replace
    if [[ "$replace" =~ ^[Yy]$ ]]; then
        echo "  → Will replace existing credentials"
    else
        echo "  → Keeping existing credentials"
        CREDENTIALS_FILE=""
    fi
else
    echo "⚠ No credentials.json found"
    echo "  Expected location: $CREDENTIALS_FILE"
fi

# Check for existing token
if [ -f "$TOKEN_FILE" ]; then
    echo "⚠ Found existing token.json"
    echo "  Location: $TOKEN_FILE"
    echo "  This token references a deleted OAuth client and should be removed."
    read -p "  Delete old token file? (Y/n): " delete_token
    if [[ ! "$delete_token" =~ ^[Nn]$ ]]; then
        rm "$TOKEN_FILE"
        echo "  ✓ Deleted old token file"
    else
        echo "  → Keeping token file (may cause issues)"
    fi
else
    echo "✓ No token.json found (will be created after authentication)"
fi

echo ""
echo "Next Steps:"
echo "==========="
echo ""
echo "1. Create OAuth credentials in Google Cloud Console:"
echo "   https://console.cloud.google.com/apis/credentials"
echo ""
echo "2. Steps:"
echo "   a. Enable Gmail API"
echo "   b. Configure OAuth consent screen"
echo "   c. Create OAuth client ID (Desktop app)"
echo "   d. Download credentials JSON"
echo ""
echo "3. Place the downloaded file at:"
echo "   $CREDENTIALS_FILE"
echo ""
echo "4. Restart Cursor - the server will prompt for authentication"
echo ""
echo "For detailed instructions, see:"
echo "   mcp/gmail-oauth-setup.md"
echo ""

# Check if credentials file was just downloaded
if [ -f ~/Downloads/*client*.json ] || [ -f ~/Downloads/*oauth*.json ] || [ -f ~/Downloads/*credentials*.json ]; then
    FOUND_FILE=$(find ~/Downloads -maxdepth 1 -name "*client*.json" -o -name "*oauth*.json" -o -name "*credentials*.json" 2>/dev/null | head -1)
    if [ -n "$FOUND_FILE" ]; then
        echo "📥 Found downloaded credentials file: $FOUND_FILE"
        read -p "   Copy to server directory? (Y/n): " copy_file
        if [[ ! "$copy_file" =~ ^[Nn]$ ]]; then
            cp "$FOUND_FILE" "$CREDENTIALS_FILE"
            echo "   ✓ Copied to: $CREDENTIALS_FILE"
            echo ""
            echo "   Next: Restart Cursor to authenticate"
        fi
    fi
fi
