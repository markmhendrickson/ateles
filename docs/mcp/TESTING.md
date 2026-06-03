# MCP Proxy Testing

Automated tests for verifying MCP proxy functionality.

## Quick Test

Fast test that verifies basic proxy functionality:

```bash
./execution/scripts/test_mcp_proxy_quick.sh
```

**Tests:**
- ✓ Proxy starts successfully
- ✓ Authentication works (returns 401 without token)
- ✓ Stdio proxy initializes
- ✓ Bearer token authentication

**Duration:** ~10-15 seconds

## Full Test Suite

Comprehensive test that verifies all functionality:

```bash
./execution/scripts/test_mcp_proxy.sh
```

**Tests:**
- Prerequisites check (python3, aiohttp, npx, server script)
- Bearer token authentication
  - Unauthorized requests (401)
  - Valid token requests (200/201)
  - Invalid token requests (401)
- OAuth 2.0 authentication
  - Token endpoint (`/oauth/token`)
  - Access token generation
  - Access token authentication
  - Invalid credentials (401)
  - Authorization endpoint (`/oauth/authorize`)

**Duration:** ~30-60 seconds

## Test Ports

Tests use non-conflicting ports:
- **Test HTTP port**: 8090
- **Test stdio proxy port**: 8091

These ports are automatically cleaned up after tests complete.

## Running Tests

### Before Running

Ensure no other services are using test ports:
```bash
lsof -ti :8090 :8091 | xargs kill -9 2>/dev/null || true
```

### Quick Test
```bash
./execution/scripts/test_mcp_proxy_quick.sh
```

### Full Test Suite
```bash
./execution/scripts/test_mcp_proxy.sh
```

## Test Output

**Success:**
```
✓ PASS: python3 found
✓ PASS: aiohttp installed
✓ PASS: Proxy process started
✓ PASS: Proxy listening on port 8090
✓ PASS: Returns 401 without token
✓ PASS: Authenticated request succeeds
```

**Failure:**
```
✗ FAIL: Expected 200/201, got 502
```

## Troubleshooting

### Proxy Fails to Start

1. **Check aiohttp:**
   ```bash
   python3 -c "import aiohttp; print('OK')"
   ```

2. **Check ports:**
   ```bash
   lsof -i :8090 :8091
   ```

3. **Check logs:**
   ```bash
   tail -50 /tmp/mcp_proxy_test.log
   ```

### Stdio Proxy Not Starting

1. **Check npx:**
   ```bash
   npx --version
   ```

2. **Check mcp-proxy:**
   ```bash
   npx -y mcp-proxy --help
   ```

3. **Check server script:**
   ```bash
   ls -la mcp/parquet/parquet_mcp_server.py
   ```

### Authentication Failing

1. **Verify token format:**
   - Bearer token: `Authorization: Bearer <token>`
   - OAuth token: Get from `/oauth/token` endpoint first

2. **Check proxy logs:**
   ```bash
   tail -f /tmp/mcp_proxy_test.log
   ```

## Continuous Integration

To run tests in CI:

```bash
# Install dependencies
pip3 install --break-system-packages aiohttp || pip3 install --user aiohttp

# Run quick test
./execution/scripts/test_mcp_proxy_quick.sh

# Or run full suite
./execution/scripts/test_mcp_proxy.sh
```

## Test Coverage

**Quick Test:**
- [x] Proxy startup
- [x] Unauthorized access (401)
- [x] Stdio proxy initialization
- [x] Bearer token authentication

**Full Test Suite:**
- [x] All quick test items
- [x] Invalid token handling
- [x] OAuth token endpoint
- [x] OAuth access token authentication
- [x] OAuth invalid credentials
- [x] OAuth authorization endpoint

## Notes

- Tests use isolated ports (8090, 8091) to avoid conflicts
- Tests automatically clean up processes on exit
- Tests generate random tokens/credentials for each run
- Full MCP protocol tests may take longer due to server initialization
