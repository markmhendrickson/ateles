# OAuth Credentials Sync from 1Password to .env

**MANDATORY:** All OAuth credentials must be synced from 1Password to `.env` via `op_sync_env_from_1password.py`. Code should **never** read directly from 1Password.

## Workflow

1. **Store credentials in 1Password:**
   - Item: `Parquet MCP Proxy` (or `MCP Proxy` for generic)
   - Fields: `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`

2. **Add mappings to env_var_mappings.parquet:**
   ```python
   # Use MCP parquet server to add mappings
   {
       "env_var": "MCP_OAUTH_CLIENT_ID",
       "op_reference": "op://Private/Parquet MCP Proxy/OAUTH_CLIENT_ID"
   }
   {
       "env_var": "MCP_OAUTH_CLIENT_SECRET",
       "op_reference": "op://Private/Parquet MCP Proxy/OAUTH_CLIENT_SECRET"
   }
   {
       "env_var": "MCP_OAUTH_REDIRECT_URI",
       "op_reference": "op://Private/Parquet MCP Proxy/OAUTH_REDIRECT_URI"
   }
   ```

3. **Sync to .env:**
   ```bash
   python execution/scripts/op_sync_env_from_1password.py
   ```

4. **Code reads from .env:**
   - All scripts automatically read from environment variables
   - `.env` file is loaded by Python scripts using `dotenv`
   - No direct 1Password reads in code

## Environment Variables

**Generic (shared across all MCP servers):**
- `MCP_OAUTH_CLIENT_ID`
- `MCP_OAUTH_CLIENT_SECRET`
- `MCP_OAUTH_REDIRECT_URI`
- `MCP_OAUTH_BASE_URL` (optional, defaults to `https://dev.neotoma.io/mcp`)

**Server-specific (for parquet server):**
- `MCP_PARQUET_OAUTH_CLIENT_ID`
- `MCP_PARQUET_OAUTH_CLIENT_SECRET`
- `MCP_PARQUET_OAUTH_REDIRECT_URI`

**Priority:** Server-specific env vars take precedence over generic ones.

## Adding Mappings

Use the MCP parquet server to add OAuth credential mappings:

```python
# Add mapping for OAuth Client ID
mcp_parquet_add_record(
    data_type="env_var_mappings",
    record={
        "env_var": "MCP_OAUTH_CLIENT_ID",
        "op_reference": "op://Private/Parquet MCP Proxy/OAUTH_CLIENT_ID",
        "description": "OAuth 2.0 Client ID for MCP proxy authentication",
        "environment_based": False,
    }
)

# Add mapping for OAuth Client Secret
mcp_parquet_add_record(
    data_type="env_var_mappings",
    record={
        "env_var": "MCP_OAUTH_CLIENT_SECRET",
        "op_reference": "op://Private/Parquet MCP Proxy/OAUTH_CLIENT_SECRET",
        "description": "OAuth 2.0 Client Secret for MCP proxy authentication",
        "environment_based": False,
    }
)

# Add mapping for OAuth Redirect URI
mcp_parquet_add_record(
    data_type="env_var_mappings",
    record={
        "env_var": "MCP_OAUTH_REDIRECT_URI",
        "op_reference": "op://Private/Parquet MCP Proxy/OAUTH_REDIRECT_URI",
        "description": "OAuth 2.0 Redirect URI for authorization code flow",
        "environment_based": False,
    }
)
```

## Verification

After syncing, verify credentials are in `.env`:

```bash
# Check .env file (credentials should be present)
grep MCP_OAUTH .env

# Or check environment variables
echo "Client ID: ${MCP_OAUTH_CLIENT_ID:-not set}"
echo "Client Secret: ${MCP_OAUTH_CLIENT_SECRET:+set (hidden)}"
```

## Code Changes

All code has been updated to:
- ✅ Read from environment variables only (`.env` file)
- ❌ **Removed** direct 1Password reads
- ✅ Prompt user to sync if credentials not found

**Files updated:**
- `execution/scripts/mcp_authenticated_proxy.py` - Removed 1Password fallback
- `execution/scripts/setup_mcp_server_tunnel.sh` - Removed 1Password fallback, added sync prompt
- `execution/scripts/test_oauth_simple.sh` - Removed 1Password fallback

## Why This Approach?

1. **Consistency:** All credentials follow the same sync workflow
2. **Security:** `.env` file is gitignored, credentials never in code
3. **Simplicity:** One sync command updates all credentials
4. **Traceability:** Mappings stored in parquet file for audit
5. **Automation:** Can be automated in setup scripts

## Related Documentation

- `execution/scripts/op_sync_env_from_1password.py` - Sync script
- `mcp/OAUTH_ENV_SETUP.md` - Environment variable setup guide
- `docs/credential_management.md` - General credential management
