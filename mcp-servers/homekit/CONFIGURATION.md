# HomeKit MCP Server - Configuration Guide

Step-by-step instructions for configuring the HomeKit MCP server.

## Step 1: Determine Your Setup

First, identify which bridge you're using:

- **Home Assistant** (most common) - If you have a Home Assistant instance
- **Homebridge** - If you're using Homebridge as a bridge
- **Native HomeKit** - If you have direct HomeKit HTTP API access (advanced)

For Legrand/Netatmo devices, you likely have either:
- Home Assistant with HomeKit integration
- Homebridge with Netatmo plugin

## Step 2: Get Your API Credentials

### Option A: Home Assistant (Recommended)

1. **Access Home Assistant**:
   - Open your Home Assistant instance in a browser
   - URL is typically: `http://homeassistant.local:8123` or `http://[your-ip]:8123`

2. **Create Long-Lived Access Token**:
   - Click on your profile/name in the bottom left sidebar
   - Scroll down to find **"Long-Lived Access Tokens"** section
   - Click **"Create Token"**
   - Give it a descriptive name: `HomeKit MCP Server`
   - Click **"OK"**
   - **IMPORTANT**: Copy the token immediately - you won't be able to see it again!
   - Save it securely (we'll use it in the next steps)

3. **Note Your Home Assistant URL**:
   - Your Home Assistant URL (e.g., `http://homeassistant.local:8123`)
   - The API endpoint will be: `http://homeassistant.local:8123/api`

### Option B: Homebridge

1. **Enable Homebridge API**:
   - Open Homebridge UI (typically `http://localhost:8581`)
   - Go to Settings → API
   - Enable API access
   - Note the API URL and token

2. **Get API Credentials**:
   - API URL is typically: `http://localhost:51826`
   - Copy the API token

## Step 3: Choose Authentication Method

You have three options (in priority order):

### Method 1: Config Directory `.env` File (Recommended for Portability)

This stores credentials in `~/.config/homekit-mcp/.env` and works across all terminals.

1. **Create the config directory and file**:
   ```bash
   mkdir -p ~/.config/homekit-mcp
   ```

2. **Create the `.env` file**:
   ```bash
   nano ~/.config/homekit-mcp/.env
   ```

3. **Add your credentials** (for Home Assistant):
   ```
   HOMEKIT_API_URL=http://homeassistant.local:8123/api
   HOMEKIT_API_TOKEN=your-long-lived-access-token-here
   HOMEKIT_BRIDGE_TYPE=homeassistant
   ```

   Or for Homebridge:
   ```
   HOMEKIT_API_URL=http://localhost:51826
   HOMEKIT_API_TOKEN=your-homebridge-token-here
   HOMEKIT_BRIDGE_TYPE=homebridge
   ```

4. **Save and exit** (in nano: `Ctrl+X`, then `Y`, then `Enter`)

5. **Set secure permissions**:
   ```bash
   chmod 600 ~/.config/homekit-mcp/.env
   ```

### Method 2: Environment Variables (For Testing)

Set environment variables in your current shell session:

```bash
export HOMEKIT_API_URL="http://homeassistant.local:8123/api"
export HOMEKIT_API_TOKEN="your-token-here"
export HOMEKIT_BRIDGE_TYPE="homeassistant"
```

**Note**: These only last for the current terminal session. Use Method 1 for permanent configuration.

### Method 3: 1Password Integration (Optional)

1. **Create 1Password Item**:
   - Title: `HomeKit` or `Home Assistant`
   - Add fields:
     - `api_url`: Your API URL (e.g., `http://homeassistant.local:8123/api`)
     - `api_token`: Your long-lived access token
     - `bridge_type`: `homeassistant` or `homebridge`

2. The MCP server will automatically detect and use these credentials if available.

## Step 4: Configure Cursor MCP Settings

1. **Open Cursor MCP Configuration**:
   - Cursor Settings → MCP (or edit `~/.cursor/mcp.json` directly)

2. **Add HomeKit Server Configuration**:

   **Option A: Using Config Directory (Recommended)**:
   ```json
   {
     "mcpServers": {
       "homekit": {
         "command": "python3",
         "args": [
           "${REPO_ROOT}/mcp/homekit/homekit_mcp_server.py"
         ],
         "env": {}
       }
     }
   }
   ```
   This uses credentials from `~/.config/homekit-mcp/.env`

   **Option B: Using Environment Variables in Config**:
   ```json
   {
     "mcpServers": {
       "homekit": {
         "command": "python3",
         "args": [
           "${REPO_ROOT}/mcp/homekit/homekit_mcp_server.py"
         ],
         "env": {
           "HOMEKIT_API_URL": "http://homeassistant.local:8123/api",
           "HOMEKIT_API_TOKEN": "your-token-here",
           "HOMEKIT_BRIDGE_TYPE": "homeassistant"
         }
       }
     }
   }
   ```

3. **Replace `${REPO_ROOT}`** with your actual repository path:
   - Full path: `/Users/markmhendrickson/repos/ateles`
   - Or use `${REPO_ROOT}` if Cursor supports variable expansion

4. **Restart Cursor** to load the new MCP server configuration.

## Step 5: Test the Configuration

1. **Test API Connection** (from terminal):
   ```bash
   # For Home Assistant
   curl -H "Authorization: Bearer YOUR_TOKEN" \
        http://homeassistant.local:8123/api/states | head -20
   ```

   You should see JSON data with your Home Assistant entities.

2. **Test MCP Server** (in Cursor):
   - Ask the agent: "List my HomeKit accessories"
   - Or: "What HomeKit devices do I have?"
   - The agent should be able to query your devices

3. **Verify Authentication**:
   - If you get authentication errors, double-check:
     - Token is correct (no extra spaces)
     - URL is correct (includes `/api` for Home Assistant)
     - Home Assistant/Homebridge is running and accessible

## Step 6: Troubleshooting

### Common Issues

1. **"HomeKit API URL not configured"**:
   - Check that `HOMEKIT_API_URL` is set correctly
   - Verify the `.env` file exists and has correct format
   - Check environment variables: `echo $HOMEKIT_API_URL`

2. **"401 Unauthorized"**:
   - Token may be incorrect or expired
   - Generate a new token in Home Assistant
   - Check token has no extra spaces or quotes

3. **"Connection refused"**:
   - Home Assistant/Homebridge may not be running
   - URL may be incorrect (check IP address or hostname)
   - Firewall may be blocking the connection

4. **"404 Not Found"**:
   - API endpoint may be wrong
   - For Home Assistant, ensure URL ends with `/api`
   - Check Home Assistant is accessible at that URL

### Debug Steps

1. **Check credentials are loaded**:
   ```bash
   python3 -c "
   import os
   print('API URL:', os.getenv('HOMEKIT_API_URL'))
   print('Bridge Type:', os.getenv('HOMEKIT_BRIDGE_TYPE'))
   print('Token set:', 'Yes' if os.getenv('HOMEKIT_API_TOKEN') else 'No')
   "
   ```

2. **Test Home Assistant API directly**:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" \
        http://homeassistant.local:8123/api/states | jq '.[0]'
   ```

3. **Check MCP server logs**:
   - Look for error messages in Cursor's MCP server output
   - Check terminal where Cursor is running

## Step 7: Verify Device Access

Once configured, test accessing your Legrand/Netatmo devices:

1. **List all accessories**:
   - Ask agent: "List all my HomeKit accessories"
   - Should show your Netatmo devices

2. **Control a light**:
   - Ask agent: "Turn on the living room light"
   - Or: "Set bedroom light to 50% brightness"

3. **Check device status**:
   - Ask agent: "What's the status of [device name]?"

## Next Steps

After successful configuration:
1. ✅ Configuration phase complete
2. ⏭️ Move to testing phase:
   - Test all MCP tools with real devices
   - Verify error handling
   - Test with different device types (lights, outlets, switches)
   - Verify scene activation works

## Security Best Practices

- ✅ Store tokens in `~/.config/homekit-mcp/.env` (not in git)
- ✅ Use `chmod 600` on `.env` file
- ✅ Use long-lived tokens (not session tokens)
- ✅ Rotate tokens periodically
- ✅ Use HTTPS if accessing Home Assistant remotely
- ✅ Consider using 1Password for token storage

## Configuration Checklist

- [ ] Determined bridge type (Home Assistant/Homebridge)
- [ ] Created long-lived access token
- [ ] Created `~/.config/homekit-mcp/.env` file
- [ ] Added credentials to `.env` file
- [ ] Set secure permissions on `.env` file
- [ ] Added HomeKit server to Cursor MCP config
- [ ] Restarted Cursor
- [ ] Tested API connection with curl
- [ ] Tested MCP server in Cursor
- [ ] Verified device access works
