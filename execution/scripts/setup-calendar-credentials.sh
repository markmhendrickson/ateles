#!/bin/bash
# Script to find and configure Google Calendar OAuth credentials

CREDENTIALS_DIR="$HOME/.cursor/mcp-tokens"
TARGET_FILE="$CREDENTIALS_DIR/gcp-oauth.keys.json"

# Create directory if it doesn't exist
mkdir -p "$CREDENTIALS_DIR"

# Check if file already exists
if [ -f "$TARGET_FILE" ]; then
    echo "✓ Credentials file already exists at: $TARGET_FILE"
    exit 0
fi

# Search for downloaded credentials file
echo "Searching for downloaded credentials file..."

# Check Downloads folder
FOUND_FILE=$(find ~/Downloads -name "*client*.json" -o -name "*oauth*.json" -o -name "*credentials*.json" 2>/dev/null | head -1)

if [ -z "$FOUND_FILE" ]; then
    # Check Desktop
    FOUND_FILE=$(find ~/Desktop -name "*client*.json" -o -name "*oauth*.json" -o -name "*credentials*.json" 2>/dev/null | head -1)
fi

if [ -z "$FOUND_FILE" ]; then
    # Check recent JSON files
    FOUND_FILE=$(find ~/Downloads -type f -name "*.json" -mtime -1 2>/dev/null | head -1)
fi

if [ -n "$FOUND_FILE" ]; then
    echo "Found credentials file: $FOUND_FILE"
    cp "$FOUND_FILE" "$TARGET_FILE"
    echo "✓ Copied to: $TARGET_FILE"
    
    # Verify it's a valid OAuth credentials file
    if grep -q "installed" "$TARGET_FILE" || grep -q "client_id" "$TARGET_FILE"; then
        echo "✓ File appears to be valid OAuth credentials"
        echo ""
        echo "Next steps:"
        echo "1. Restart Cursor"
        echo "2. The MCP server will prompt for authentication on first use"
    else
        echo "⚠ Warning: File may not be valid OAuth credentials"
    fi
else
    echo "No credentials file found. Please:"
    echo "1. Go to: https://console.cloud.google.com/apis/credentials"
    echo "2. Click 'Create Credentials' → 'OAuth client ID'"
    echo "3. Select 'Desktop app'"
    echo "4. Download the JSON file"
    echo "5. Run this script again"
fi








