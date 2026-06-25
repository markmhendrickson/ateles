# MCP Server Submodules

All MCP servers are managed as git submodules in `.gitmodules`. This ensures:
- Version control of server code
- Easy updates via `git submodule update`
- Consistent installation across environments

## Initializing Submodules

### All Submodules
```bash
./scripts/init_submodules.sh
```

### Specific Submodule
```bash
git submodule update --init mcp/gmail
```

### Check Status
```bash
./scripts/ensure_submodules.sh
```

## Wrapper Scripts

Wrapper scripts in `mcp/*/run-*-mcp.sh` automatically:
1. Check if submodule is initialized
2. Load environment variables from `.env`
3. Set credential paths to repo `.creds/` directory
4. Handle server-specific setup (Node.js vs Python)

## Available MCP Server Submodules

- `mcp/gmail` - Gmail integration (Node.js/TypeScript)
- `mcp/google-calendar` - Google Calendar integration
- `mcp/instagram` - Instagram Business API
- `mcp/dnsimple` - DNSimple domain management
- `mcp/minted` - Minted.com API
- `mcp/parquet` - Parquet data access (Truth Layer)
- `mcp/asana` - Asana project management

## Updating Submodules

To update all submodules to latest:
```bash
git submodule update --remote
```

To update a specific submodule:
```bash
git submodule update --remote mcp/gmail
cd mcp/gmail
git checkout main  # or appropriate branch
cd ../..
git add mcp/gmail
git commit -m "Update gmail submodule"
```
