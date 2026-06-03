# OAuth Environment Variable Setup

OAuth credentials should **never be hardcoded**. Use environment variables or 1Password.

## Environment Variables

### Option 1: Generic (Shared Across All MCP Servers)

Add to `.env` file or export in shell:

```bash
export MCP_OAUTH_CLIENT_ID='your-client-id'
export MCP_OAUTH_CLIENT_SECRET='your-client-secret'
export MCP_OAUTH_REDIRECT_URI='https://dev.neotoma.io/mcp/oauth/callback'
export MCP_OAUTH_BASE_URL='https://dev.neotoma.io/mcp'
```

### Option 2: Server-Specific

For server-specific credentials (e.g., parquet):

```bash
export MCP_PARQUET_OAUTH_CLIENT_ID='your-client-id'
export MCP_PARQUET_OAUTH_CLIENT_SECRET='your-client-secret'
export MCP_PARQUET_OAUTH_REDIRECT_URI='https://dev.neotoma.io/mcp/oauth/callback'
```

**Priority:** Server-specific env vars take precedence over generic ones.

## 1Password Storage

Store in 1Password for secure credential management:

**Item Name:** `Parquet MCP Proxy` (server-specific) or `MCP Proxy` (generic)

**Fields:**
- `OAUTH_CLIENT_ID` - OAuth client identifier
- `OAUTH_CLIENT_SECRET` - OAuth client secret
- `OAUTH_REDIRECT_URI` - OAuth redirect URI (optional, can use env var)

**1Password Paths Checked:**
1. `op://Private/{ServerName} MCP Proxy/OAUTH_CLIENT_ID` (server-specific)
2. `op://Private/MCP Proxy/OAUTH_CLIENT_ID` (generic)

## Usage

### Starting MCP Proxy with OAuth

The proxy script automatically checks environment variables and 1Password:

```bash
# OAuth credentials from environment
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080

# Or explicitly pass (overrides env vars)
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080 \
    --oauth-client-id "$MCP_OAUTH_CLIENT_ID" \
    --oauth-client-secret "$MCP_OAUTH_CLIENT_SECRET" \
    --oauth-redirect-uri "$MCP_OAUTH_REDIRECT_URI"
```

### Testing OAuth

The test script automatically uses environment variables:

```bash
# Set credentials
export MCP_OAUTH_CLIENT_ID='your-client-id'
export MCP_OAUTH_CLIENT_SECRET='your-client-secret'

# Run test
./execution/scripts/test_oauth_simple.sh
```

## Security Best Practices

1. **Never commit credentials to git** - Use `.env` file (in `.gitignore`)
2. **Use 1Password for production** - More secure than `.env` files
3. **Rotate credentials regularly** - Update both env vars and 1Password
4. **Use server-specific credentials** - Different credentials per server for isolation

## Adding to .env File

If using `.env` file (recommended for development):

```bash
# Add to .env file
echo "MCP_OAUTH_CLIENT_ID=your-client-id" >> .env
echo "MCP_OAUTH_CLIENT_SECRET=your-client-secret" >> .env
echo "MCP_OAUTH_REDIRECT_URI=https://dev.neotoma.io/mcp/oauth/callback" >> .env
echo "MCP_OAUTH_BASE_URL=https://dev.neotoma.io/mcp" >> .env

# Load in shell
source .env
```

## Verification

Check that credentials are loaded:

```bash
# Check environment variables
echo "Client ID: ${MCP_OAUTH_CLIENT_ID:-not set}"
echo "Client Secret: ${MCP_OAUTH_CLIENT_SECRET:+set (hidden)}"

# Test OAuth endpoint
./execution/scripts/test_oauth_simple.sh
```
