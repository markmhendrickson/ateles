# Gmail MCP Server OAuth Setup Guide

## Problem
The Gmail MCP server is failing with error: `deleted_client: The OAuth client was deleted.`

This means the OAuth client credentials used by the server have been deleted from Google Cloud Console.

## Solution: Create New OAuth Credentials

### Step 1: Create OAuth Credentials in Google Cloud Console

1. **Go to Google Cloud Console**
   - Visit: https://console.cloud.google.com/
   - Select your project (or create a new one)

2. **Enable Gmail API**
   - Go to "APIs & Services" > "Library"
   - Search for "Gmail API"
   - Click "Enable"

3. **Configure OAuth Consent Screen** (if not already done)
   - Go to "APIs & Services" > "OAuth consent screen"
   - If prompted, click "Get started"
   - Fill in:
     - App name: "MCP Gmail Integration"
     - User support email: Your email
     - Choose "External" user type
   - Click "Save and Continue" through the steps
   - Click "Back to Dashboard"

4. **Create OAuth Client ID**
   - Go to "APIs & Services" > "Credentials"
   - Click "+ CREATE CREDENTIALS" > "OAuth client ID"
   - Application type: Select "Desktop app"
   - Name: "MCP Gmail Desktop Client"
   - Click "CREATE"
   - Click "DOWNLOAD JSON" in the dialog
   - Save the file as `credentials.json`

5. **Add Required Scopes**
   - Go to "APIs & Services" > "OAuth consent screen"
   - Click "Edit App"
   - Go to "Scopes" tab
   - Click "+ ADD OR REMOVE SCOPES"
   - Search for and add:
     - `https://www.googleapis.com/auth/gmail.readonly`
     - `https://www.googleapis.com/auth/gmail.send`
     - `https://www.googleapis.com/auth/gmail.compose`
     - `https://www.googleapis.com/auth/gmail.labels`
   - Or add the combined scope: `https://www.googleapis.com/auth/gmail.modify`
   - Click "UPDATE" and "SAVE AND CONTINUE"

### Step 2: Place Credentials File

1. Copy the downloaded `credentials.json` file to:
   ```
   ~/.local/mcp-servers/mcp-gmail/credentials.json
   ```

2. Or set the environment variable in your MCP configuration:
   ```json
   {
     "mcpServers": {
       "gmail": {
         "command": "/Users/markmhendrickson/.local/mcp-servers/mcp-gmail/venv/bin/python",
         "args": ["/Users/markmhendrickson/.local/mcp-servers/mcp-gmail/run_server.py"],
         "env": {
           "MCP_GMAIL_CREDENTIALS_PATH": "/path/to/your/credentials.json",
           "MCP_GMAIL_TOKEN_PATH": "/path/to/your/token.json"
         }
       }
     }
   }
   ```

### Step 3: Remove Old Token (if exists)

The old token file references the deleted OAuth client. Delete it to force re-authentication:

```bash
rm ~/.local/mcp-servers/mcp-gmail/token.json
```

Or if using a custom path:
```bash
rm /path/to/your/token.json
```

### Step 4: Restart Cursor

After placing the credentials file and removing the old token:

1. Restart Cursor completely
2. The MCP server will automatically prompt for OAuth authentication on first use
3. A browser window will open for you to authorize the application
4. After authorization, a new `token.json` file will be created

### Step 5: Verify Setup

The server should now work correctly. You can test by:
- Using Gmail MCP tools in Cursor
- Checking that the server starts without errors

## Troubleshooting

### If authentication fails:
- Make sure the credentials.json file is valid JSON
- Verify the OAuth client is not deleted in Google Cloud Console
- Check that all required scopes are added to the OAuth consent screen
- Ensure you're using the correct Google account

### If token refresh fails:
- Delete the token.json file and re-authenticate
- Verify the OAuth client still exists in Google Cloud Console
- Check that the credentials.json file matches the OAuth client

## Environment Variables

You can configure the server using these environment variables:

- `MCP_GMAIL_CREDENTIALS_PATH`: Path to credentials.json (default: `credentials.json` in server directory)
- `MCP_GMAIL_TOKEN_PATH`: Path to token.json (default: `token.json` in server directory)
- `MCP_GMAIL_MAX_RESULTS`: Default max results for searches (default: 10)
