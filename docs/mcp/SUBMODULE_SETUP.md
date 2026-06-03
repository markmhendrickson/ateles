# MCP Server Submodule Setup

All MCP servers are configured as git submodules in `.gitmodules`. Wrapper scripts in `mcp/*/run-*-mcp.sh` properly handle submodule initialization.

## Current Status

✅ **Gmail** - Initialized and working
- Wrapper: `mcp/gmail/run-gmail-mcp.sh`
- Checks for submodule initialization
- Loads `.env` and sets credentials to `.creds/`

⚠️ **Google Calendar** - Configured but not initialized
- Wrapper: `mcp/google-calendar/run-google-calendar-mcp.sh`
- Will check for initialization and provide helpful errors

## Wrapper Script Features

All wrapper scripts:
1. ✅ Check if submodule is initialized (has `.git` directory or server files)
2. ✅ Load environment variables from `.env` file
3. ✅ Set credential/token paths to repo `.creds/` directory (never `~/.local`)
4. ✅ Provide helpful error messages if submodule not initialized
5. ✅ Handle both Node.js and Python servers appropriately

## Initializing Submodules

### All at Once
```bash
./scripts/init_submodules.sh
```

### Check Status
```bash
./scripts/ensure_submodules.sh
```

### Individual Submodule
```bash
git submodule update --init mcp/google-calendar
```

## Submodules in .gitmodules

- `mcp/gmail` - ✅ Initialized
- `mcp/google-calendar` - ⚠️ Needs initialization
- `mcp/instagram` - ⚠️ Needs initialization
- `mcp/dnsimple` - ⚠️ Needs initialization
- `mcp/minted` - ⚠️ Needs initialization
- `mcp/parquet` - ⚠️ Needs initialization
- `mcp/asana` - ⚠️ Needs initialization

## Notes

- Wrapper scripts work even if submodules aren't initialized (they provide helpful errors)
- All credentials/tokens stored in repo `.creds/` directory
- Never defaults to `~/.local/` paths
