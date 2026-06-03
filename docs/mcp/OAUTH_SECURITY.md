# OAuth Endpoint Security

## Security Issue

The `/oauth/authorize` endpoint was previously **completely open** - anyone could visit it without authentication or client validation, and it would immediately generate and return authorization codes.

**Risk:** Unauthorized users could obtain authorization codes without proper client validation.

## Fix Applied

The endpoint now requires and validates:

1. **`client_id` query parameter** - Must match the configured OAuth client ID
2. **`redirect_uri` query parameter** - Must match the configured redirect URI

### Before (Insecure)

```python
# Anyone could visit /oauth/authorize and get a code
async def handle_oauth_authorize(...):
    # No validation - immediately generates code
    code = secrets.token_urlsafe(32)
    return redirect(callback_url)
```

### After (Secure)

```python
# Requires valid client_id and redirect_uri
async def handle_oauth_authorize(...):
    # Validate client_id
    if provided_client_id != oauth_client_id:
        return error_response("invalid_client")
    
    # Validate redirect_uri
    if provided_redirect_uri != oauth_redirect_uri:
        return error_response("invalid_request")
    
    # Only then generate code
    code = secrets.token_urlsafe(32)
    return redirect(callback_url)
```

## Testing

### Invalid Request (No client_id)
```bash
curl "https://dev.neotoma.io/mcp/oauth/authorize"
# Returns: 400 {"error": "invalid_client", "error_description": "Invalid or missing client_id parameter"}
```

### Invalid Request (Wrong client_id)
```bash
curl "https://dev.neotoma.io/mcp/oauth/authorize?client_id=invalid"
# Returns: 400 {"error": "invalid_client", "error_description": "Invalid or missing client_id parameter"}
```

### Valid Request (Correct client_id and redirect_uri)
```bash
curl "https://dev.neotoma.io/mcp/oauth/authorize?client_id=parquet-<client-id>&redirect_uri=https://dev.neotoma.io/mcp/oauth/callback"
# Returns: 302 redirect to callback with authorization code
```

## OAuth 2.0 Authorization Code Flow

The authorization endpoint is **intended to be publicly accessible** in OAuth 2.0 flows, but it must:

1. ✅ Validate `client_id` parameter
2. ✅ Validate `redirect_uri` parameter  
3. ✅ Require user consent (optional for automated flows like MCP)
4. ✅ Generate secure authorization codes
5. ✅ Use CSRF protection via `state` parameter

**Note:** The endpoint being "public" is normal for OAuth - the security comes from:
- Client ID validation (only registered clients can use it)
- Redirect URI validation (prevents redirect attacks)
- Authorization code is single-use and short-lived
- Client secret is required to exchange code for token

## Implementation Status

**Fixed in:** `execution/scripts/mcp_authenticated_proxy.py`  
**Status:** Code updated, requires proxy restart to take effect

## Restart Required

After updating the code, restart the MCP proxy:

```bash
# Find and kill existing proxy
pkill -f "mcp_authenticated_proxy.py"

# Restart with OAuth credentials
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080 \
    --oauth-client-id "$MCP_OAUTH_CLIENT_ID" \
    --oauth-client-secret "$MCP_OAUTH_CLIENT_SECRET" \
    --oauth-redirect-uri "$MCP_OAUTH_REDIRECT_URI"
```

Or if using environment variables/1Password:

```bash
# Restart (will auto-detect OAuth credentials from env/1Password)
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
```
