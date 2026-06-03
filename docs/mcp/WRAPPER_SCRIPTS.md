# MCP Server Wrapper Scripts

All MCP servers now use consistent wrapper scripts (`run-*-mcp.sh`) for:

1. **Consistency** - All servers use the same pattern
2. **Environment Loading** - Automatically loads `.env` file from repo root
3. **Submodule Checking** - Verifies submodules are initialized (where applicable)
4. **Venv Support** - Uses `execution/venv` if available, falls back to system Python
5. **Error Messages** - Provides helpful errors if submodules aren't initialized

## Available Wrapper Scripts

- `mcp/parquet/run-parquet-mcp.sh` - Parquet data access server
- `mcp/gmail/run-gmail-mcp.sh` - Gmail integration (Node.js)
- `mcp/dnsimple/run-dnsimple-mcp.sh` - DNSimple domain management
- `mcp/google-calendar/run-google-calendar-mcp.sh` - Google Calendar integration
- `mcp/instagram/run-instagram-mcp.sh` - Instagram Business API
- `mcp/minted/run-minted-mcp.sh` - Minted.com API
- `mcp/asana/run-asana-mcp.sh` - Asana project management
- `mcp/homekit/run-homekit-mcp.sh` - HomeKit device control
- `mcp/whatsapp/run-whatsapp-mcp.sh` - WhatsApp Business Platform

## Common Features

All wrapper scripts:

1. **Load `.env` file** - Automatically sources `.env` from repo root
2. **Check submodule initialization** - For submodule-based servers, verifies they're initialized
3. **Use venv if available** - Prefers `execution/venv/bin/python3`, falls back to system Python
4. **Provide helpful errors** - Clear messages if submodules aren't initialized

## Configuration

All servers in `mcp/mcp-config-template.json` now use wrapper scripts:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "${REPO_ROOT}/mcp/parquet/run-parquet-mcp.sh"
    },
    "gmail": {
      "command": "${REPO_ROOT}/mcp/gmail/run-gmail-mcp.sh"
    },
    // ... etc
  }
}
```

## Benefits

- **Consistent pattern** - All servers work the same way
- **Centralized .env loading** - No need to configure env vars in mcp.json
- **Submodule awareness** - Automatically checks and initializes submodules
- **Better error messages** - Clear guidance when things go wrong
- **Venv support** - Automatically uses project venv when available
