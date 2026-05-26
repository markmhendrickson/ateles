---
name: sync-env-from-1password
description: Sync .env from 1Password using env_var_mappings.
triggers:
  - sync env from 1password
  - /sync_env_from_1password
  - sync-env-from-1password
user_invocable: true
entity_id: ent_86573f752c9e63db776636ae
---

# Sync Environment Variables from 1Password

Sync environment variables from 1Password to local `.env` file using environment variable mappings stored in Neotoma as `env_var_mapping` entities.

## Command

```
sync_env_from_1password
```

or

```
sync env from 1password
```

## Purpose

This command syncs environment variables from 1Password to your local `.env` file based on `env_var_mapping` entities stored in Neotoma.

**Key features:**
- Automatic backup before modification
- Preserves unmanaged environment variables
- Never prints secret values
- Supports environment-based keys (e.g., different API keys for dev/prod)
- Uses 1Password MCP server (preferred) or CLI (fallback)

## Prerequisites

### Option 1: 1Password MCP Server (Recommended)

**Benefits:**
- Persistent connection (no session expiration)
- Better error handling
- No repeated authentication
- Consistent with other MCP integrations

**Setup:**

1. **Ensure 1Password CLI is installed:**
   ```bash
   brew install 1password-cli
   op signin
   ```

2. **Configure MCP server** in `~/.cursor/mcp.json`:
   ```json
   {
     "mcpServers": {
       "onepassword": {
         "command": "bash",
         "args": [
           "/Users/markmhendrickson/repos/ateles/mcp/onepassword/run-onepassword-mcp.sh"
         ],
         "env": {}
       }
     }
   }
   ```

3. **Verify MCP server is running:**
   - Restart Cursor after updating mcp.json
   - MCP server status visible in Cursor MCP panel

**For detailed setup instructions, see:** `mcp/onepassword/README.md`

### Option 2: 1Password CLI (Fallback)

**When to use:**
- MCP server not configured
- Temporary/one-off sync
- Troubleshooting MCP issues

**Setup:**

1. **Install 1Password CLI:**
   ```bash
   brew install 1password-cli
   ```

2. **Sign in:**
   ```bash
   op signin
   ```

**Note:** CLI sessions expire periodically and require re-authentication.

## Configuration

### Environment Variable Mappings

Mappings live in Neotoma as `env_var_mapping` entities. Retrieve via `retrieve_entities({ entity_type: "env_var_mapping" })`.

**Schema:**
- `env_var` (string): Environment variable name (e.g., `OPENAI_API_KEY`)
- `op_reference` (string): 1Password reference (e.g., `op://Personal/API-Keys/openai_token`)
- `item_name` (string, optional): Friendly name of 1Password item
- `field_label` (string, optional): Field label in 1Password
- `notes` (string, optional): Additional notes
- `environment_based` (boolean, optional): If true, key varies by environment
- `environment_key` (string, optional): Environment identifier (e.g., "development", "production")

### Adding/Updating Mappings

**Via Neotoma MCP:**
```
store_structured({
  entities: [
    {
      entity_type: "env_var_mapping",
      env_var: "MY_API_KEY",
      op_reference: "op://Personal/API-Keys/my_api_key",
      item_name: "API Keys",
      field_label: "my_api_key"
    }
  ]
})
```

To update an existing mapping, retrieve it by `env_var` and issue a `correct` (or `store_structured` on the same `env_var`, which adds a new observation).

### Environment-Based Keys

For environment-specific keys (e.g., different API keys for dev/prod):

```python
# Development key
{
    "env_var": "OPENAI_API_KEY",
    "op_reference": "op://Personal/API-Keys/openai_dev",
    "environment_based": True,
    "environment_key": "development"
}

# Production key
{
    "env_var": "OPENAI_API_KEY",
    "op_reference": "op://Personal/API-Keys/openai_prod",
    "environment_based": True,
    "environment_key": "production"
}
```

**Set environment:**
```bash
export ENVIRONMENT=production  # or "development"
```

## Execution Instructions

### Step 1: Verify Prerequisites

**If using MCP server:**
```bash
# Verify MCP server is configured (check ~/.cursor/mcp.json)
# Restart Cursor if you just added the configuration
```

**If using CLI fallback:**
```bash
# Verify 1Password CLI is installed and signed in
op whoami
# If not signed in: op signin
```

### Step 2: Run Sync Command

**From repository root:**
```bash
python execution/scripts/op_sync_env_from_1password.py
```

**Or with custom .env path:**
```bash
python execution/scripts/op_sync_env_from_1password.py /path/to/.env
```

### Step 3: Verify Results

**Check output:**
- ✓ Backup created (in `.env.backups/`)
- ✓ Updated keys listed (values NOT shown for security)
- ✓ Sync completed successfully

**Check .env file:**
```bash
# Verify variables were updated (don't print full file - contains secrets)
grep -c "=" .env  # Count of variables
```

## Behavior

### Backup

**Automatic backup before modification:**
- Location: `.env.backups/.env-YYYY-MM-DD-HHMMSS`
- Only created if `.env` file exists
- Backup preserves exact file state

### Variable Updates

**For each mapped variable:**
1. Resolve secret from 1Password (via MCP or CLI)
2. Update existing variable or append new one
3. Preserve other variables in `.env` (unmanaged variables untouched)

**Variables with `PLACEHOLDER_` prefix are skipped** until configured with actual 1Password references.

### Security

**Security guarantees:**
- Never prints secret values (only variable names)
- Creates backup before modification
- Error messages never expose secrets
- Validates session before proceeding

## Error Handling

### "1Password CLI is not authenticated"

**Problem:** Not signed in to 1Password CLI.

**Solution:**
```bash
op signin
```

### "MCP failed, falling back to CLI"

**Problem:** MCP server unavailable or misconfigured.

**Solution:**
1. Check MCP server configuration in `~/.cursor/mcp.json`
2. Restart Cursor after updating configuration
3. Verify 1Password CLI is installed and signed in
4. Script will automatically fall back to CLI

### "No env_var_mapping entities found"

**Problem:** Neotoma has no `env_var_mapping` entities yet.

**Solution:** Add mappings via Neotoma MCP `store_structured` as shown in the Configuration section above.

### "Empty value returned for 1Password ref"

**Problem:** Field exists but contains empty value.

**Solution:**
- Verify field has value in 1Password app
- Check spelling of field name in reference
- Try: `op item get "<item-name>" --format=json` to see available fields

## Related Documentation

- `mcp/onepassword/README.md` - 1Password MCP server documentation
- `execution/scripts/onepassword_client.py` - MCP client implementation
- `execution/scripts/op_sync_env_from_1password.py` - Sync script implementation
- `list_entity_types` in Neotoma — `env_var_mapping` schema (fields)

## Notes

- **MCP preferred over CLI:** Script tries MCP first, falls back to CLI automatically
- **Persistent connection:** MCP eliminates session expiration issues
- **Backward compatible:** Existing CLI-based workflows continue to work
- **No breaking changes:** Same interface, enhanced implementation
- **Security first:** All security guarantees preserved from original implementation
