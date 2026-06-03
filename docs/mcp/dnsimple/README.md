# DNSimple MCP Server

MCP server for DNSimple API interactions, providing tools for domain management, DNS configuration, pricing queries, domain transfers, and DNSSEC management.

## Features

- **Domain Management**: List domains, get pricing, check renewal costs
- **DNS Configuration**: Create, update, list, and delete DNS records
- **DNSSEC Management**: Enable/disable DNSSEC, list/create/get/delete delegation signer (DS) records
- **Auto-Renewal Control**: Disable auto-renewal for domains
- **Domain Transfers**: Initiate domain transfers to DNSimple

## Installation

```bash
cd truth/mcp-servers/dnsimple
pip install -r requirements.txt
```

## Configuration

### Authentication

The server supports two authentication methods:

1. **Environment Variable** (recommended for CI/CD):
   ```bash
   export DNSIMPLE_API_TOKEN="your-token-here"
   ```

2. **1Password Integration** (recommended for local development):
   - Configure 1Password item titled "DNSimple" or with URL "dnsimple.com"
   - Add field: "access token", "api_token", or "token" with your DNSimple API token
   - Get your API token from: https://dnsimple.com/user

The token is automatically cached to `.env` file for future use.

### Cursor Configuration

Add to your Cursor MCP settings (typically `~/.cursor/mcp.json` or Cursor settings):

```json
{
  "mcpServers": {
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/truth/mcp-servers/dnsimple/dnsimple_mcp_server.py"
      ],
      "env": {
        "DNSIMPLE_API_TOKEN": "your-token-here"
      }
    }
  }
}
```

Or use 1Password integration (recommended):

```json
{
  "mcpServers": {
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/truth/mcp-servers/dnsimple/dnsimple_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

### Claude Desktop Configuration

Add to `claude_desktop_config.json` (typically `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/truth/mcp-servers/dnsimple/dnsimple_mcp_server.py"
      ]
    }
  }
}
```

## Available Tools

### `list_domains`

List all domains in the DNSimple account.

**Parameters:** None

**Returns:**
- `account_id`: DNSimple account ID
- `count`: Number of domains
- `domains`: Array of domain objects with name, expires_at, auto_renew, etc.

**Example:**
```json
{
  "account_id": "12345",
  "count": 5,
  "domains": [
    {
      "id": 123456,
      "name": "example.com",
      "expires_at": "2026-12-31",
      "auto_renew": true,
      "registrant_id": 789
    }
  ]
}
```

### `get_domain_costs`

Get pricing information for domains. Returns registration and renewal costs.

**Parameters:**
- `domain_names` (optional): Array of domain names to get pricing for. If empty, returns pricing for all domains in account.

**Returns:**
- `account_id`: DNSimple account ID
- `domains`: Array of domain pricing objects

**Example:**
```json
{
  "domain_names": ["example.com", "test.org"]
}
```

**Response:**
```json
{
  "account_id": "12345",
  "domains": [
    {
      "domain": "example.com",
      "domain_info": {
        "id": 123456,
        "name": "example.com",
        "expires_at": "2026-12-31"
      },
      "prices": [
        {
          "operation": "register",
          "price": "15.00",
          "currency": "USD"
        },
        {
          "operation": "renew",
          "price": "15.00",
          "currency": "USD"
        }
      ]
    }
  ]
}
```

### `get_renewal_costs`

Get renewal costs for domains. Returns total annual renewal cost and per-domain breakdown.

**Parameters:**
- `domain_names` (optional): Array of domain names to get renewal costs for. If empty, returns costs for all domains in account.

**Returns:**
- `account_id`: DNSimple account ID
- `total_domains`: Number of domains
- `total_annual_renewal_cost`: Sum of all renewal costs
- `domains`: Array of domain renewal details

**Example:**
```json
{
  "domain_names": ["example.com", "test.org"]
}
```

**Response:**
```json
{
  "account_id": "12345",
  "total_domains": 2,
  "total_annual_renewal_cost": 30.00,
  "domains": [
    {
      "domain": "example.com",
      "expires_at": "2026-12-31",
      "auto_renew": true,
      "renewal_price": 15.00,
      "currency": "USD"
    },
    {
      "domain": "test.org",
      "expires_at": "2026-06-15",
      "auto_renew": false,
      "renewal_price": 15.00,
      "currency": "USD"
    }
  ]
}
```

### `list_dns_records`

List DNS records for a domain.

**Parameters:**
- `domain_name` (required): Domain name (e.g., "example.com")
- `name` (optional): Filter by record name
- `type` (optional): Filter by record type (A, AAAA, CNAME, MX, TXT, NS, SRV, ALIAS)

**Returns:**
- `domain`: Domain name
- `count`: Number of records
- `records`: Array of DNS record objects

**Example:**
```json
{
  "domain_name": "example.com",
  "type": "A"
}
```

**Response:**
```json
{
  "domain": "example.com",
  "count": 2,
  "records": [
    {
      "id": 123456,
      "name": "www",
      "type": "A",
      "content": "192.0.2.1",
      "ttl": 3600,
      "priority": null
    },
    {
      "id": 123457,
      "name": "@",
      "type": "A",
      "content": "192.0.2.1",
      "ttl": 3600,
      "priority": null
    }
  ]
}
```

### `configure_dns_record`

Create or update a DNS record. If a record with the same name and type exists, it will be updated.

**Parameters:**
- `domain_name` (required): Domain name (e.g., "example.com")
- `name` (required): Record name (e.g., "www" or "@" for root domain)
- `type` (required): DNS record type (A, AAAA, CNAME, MX, TXT, NS, SRV, ALIAS)
- `content` (required): Record content (IP address for A/AAAA, hostname for CNAME, etc.)
- `ttl` (optional): TTL in seconds (default: 3600)
- `priority` (optional): Priority for MX records

**Returns:**
- `success`: Boolean indicating success
- `action`: "created" or "updated"
- `record`: DNS record object

**Example (Create A record):**
```json
{
  "domain_name": "example.com",
  "name": "www",
  "type": "A",
  "content": "192.0.2.1",
  "ttl": 3600
}
```

**Example (Create MX record):**
```json
{
  "domain_name": "example.com",
  "name": "@",
  "type": "MX",
  "content": "mail.example.com",
  "priority": 10,
  "ttl": 3600
}
```

**Example (Create CNAME record):**
```json
{
  "domain_name": "example.com",
  "name": "blog",
  "type": "CNAME",
  "content": "example.github.io",
  "ttl": 3600
}
```

### `delete_dns_record`

Delete a DNS record by ID.

**Parameters:**
- `domain_name` (required): Domain name (e.g., "example.com")
- `record_id` (required): DNS record ID to delete

**Returns:**
- `success`: Boolean indicating success
- `message`: Success message

**Example:**
```json
{
  "domain_name": "example.com",
  "record_id": "123456"
}
```

### `disable_autorenew`

Disable auto-renewal for one or more domains.

**Parameters:**
- `domain_names` (required): Array of domain names to disable auto-renewal for

**Returns:**
- `results`: Array of operation results

**Example:**
```json
{
  "domain_names": ["example.com", "test.org"]
}
```

**Response:**
```json
{
  "results": [
    {
      "domain": "example.com",
      "status": "disabled",
      "error": null
    },
    {
      "domain": "test.org",
      "status": "disabled",
      "error": null
    }
  ]
}
```

### `transfer_domain`

Initiate a domain transfer to DNSimple. Requires authorization code from current registrar.

**Parameters:**
- `domain_name` (required): Domain name to transfer
- `auth_code` (required): Authorization code (EPP code) from current registrar
- `registrant_id` (optional): Registrant ID (uses account default if not provided)

**Returns:**
- `success`: Boolean indicating success
- `transfer`: Transfer object with ID and status

**Example:**
```json
{
  "domain_name": "example.com",
  "auth_code": "ABC123XYZ789"
}
```

**Response:**
```json
{
  "success": true,
  "transfer": {
    "id": 123456,
    "domain_id": 789012,
    "state": "new",
    "auto_renew": false,
    "whois_privacy": false
  }
}
```

**Note:** Domain must be unlocked at current registrar before transfer can be initiated.

## Error Handling

The server returns structured error messages in JSON format when operations fail. Common errors include:

- **Authentication errors**: Missing or invalid API token
- **Account errors**: No account found or account access issues
- **Domain errors**: Domain not found or not accessible
- **API errors**: DNSimple API errors with status codes and messages

**Example error response:**
```json
{
  "error": "Failed to get account ID: Invalid API response: missing 'data' key"
}
```

## Security Notes

- API tokens are never logged or exposed in error messages
- Tokens can be stored in environment variables or 1Password
- All API requests use HTTPS
- Token is automatically cached to `.env` file (with restricted permissions)

## Troubleshooting

1. **Authentication Fails**
   - Verify API token is correct
   - Check token has not expired
   - Ensure 1Password integration is configured correctly
   - Get new token from: https://dnsimple.com/user

2. **Account Not Found**
   - Verify account exists in DNSimple
   - Check account permissions
   - Ensure API token has account access

3. **Domain Not Found**
   - Verify domain exists in DNSimple account
   - Check domain spelling
   - Ensure domain is registered through DNSimple (for registrar operations)

4. **DNS Record Operations Fail**
   - Verify domain is using DNSimple nameservers
   - Check record name and type are valid
   - Ensure TTL is within valid range (60-604800 seconds)

## Notes

- The server automatically discovers the account ID from the DNSimple API
- DNS record updates will replace existing records with the same name and type
- Domain transfers require the domain to be unlocked at the current registrar
- All date fields are returned as ISO format strings
- The server runs in stdio mode for MCP communication

## License

MIT

## Support

- [GitHub Issues](https://github.com/markmhendrickson/mcp-server-dnsimple/issues)


MCP server for DNSimple API interactions, providing tools for domain management, DNS configuration, pricing queries, and domain transfers.

## Features

- **Domain Management**: List domains, get pricing, check renewal costs
- **DNS Configuration**: Create, update, list, and delete DNS records
- **Auto-Renewal Control**: Disable auto-renewal for domains
- **Domain Transfers**: Initiate domain transfers to DNSimple

## Installation

```bash
cd truth/mcp-servers/dnsimple
pip install -r requirements.txt
```

## Configuration

### Authentication

The server supports two authentication methods:

1. **Environment Variable** (recommended for CI/CD):
   ```bash
   export DNSIMPLE_API_TOKEN="your-token-here"
   ```

2. **1Password Integration** (recommended for local development):
   - Configure 1Password item titled "DNSimple" or with URL "dnsimple.com"
   - Add field: "access token", "api_token", or "token" with your DNSimple API token
   - Get your API token from: https://dnsimple.com/user

The token is automatically cached to `.env` file for future use.

### Cursor Configuration

Add to your Cursor MCP settings (typically `~/.cursor/mcp.json` or Cursor settings):

```json
{
  "mcpServers": {
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/truth/mcp-servers/dnsimple/dnsimple_mcp_server.py"
      ],
      "env": {
        "DNSIMPLE_API_TOKEN": "your-token-here"
      }
    }
  }
}
```

Or use 1Password integration (recommended):

```json
{
  "mcpServers": {
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/truth/mcp-servers/dnsimple/dnsimple_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

### Claude Desktop Configuration

Add to `claude_desktop_config.json` (typically `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/truth/mcp-servers/dnsimple/dnsimple_mcp_server.py"
      ]
    }
  }
}
```

## Available Tools

### `list_domains`

List all domains in the DNSimple account.

**Parameters:** None

**Returns:**
- `account_id`: DNSimple account ID
- `count`: Number of domains
- `domains`: Array of domain objects with name, expires_at, auto_renew, etc.

**Example:**
```json
{
  "account_id": "12345",
  "count": 5,
  "domains": [
    {
      "id": 123456,
      "name": "example.com",
      "expires_at": "2026-12-31",
      "auto_renew": true,
      "registrant_id": 789
    }
  ]
}
```

### `get_domain_costs`

Get pricing information for domains. Returns registration and renewal costs.

**Parameters:**
- `domain_names` (optional): Array of domain names to get pricing for. If empty, returns pricing for all domains in account.

**Returns:**
- `account_id`: DNSimple account ID
- `domains`: Array of domain pricing objects

**Example:**
```json
{
  "domain_names": ["example.com", "test.org"]
}
```

**Response:**
```json
{
  "account_id": "12345",
  "domains": [
    {
      "domain": "example.com",
      "domain_info": {
        "id": 123456,
        "name": "example.com",
        "expires_at": "2026-12-31"
      },
      "prices": [
        {
          "operation": "register",
          "price": "15.00",
          "currency": "USD"
        },
        {
          "operation": "renew",
          "price": "15.00",
          "currency": "USD"
        }
      ]
    }
  ]
}
```

### `get_renewal_costs`

Get renewal costs for domains. Returns total annual renewal cost and per-domain breakdown.

**Parameters:**
- `domain_names` (optional): Array of domain names to get renewal costs for. If empty, returns costs for all domains in account.

**Returns:**
- `account_id`: DNSimple account ID
- `total_domains`: Number of domains
- `total_annual_renewal_cost`: Sum of all renewal costs
- `domains`: Array of domain renewal details

**Example:**
```json
{
  "domain_names": ["example.com", "test.org"]
}
```

**Response:**
```json
{
  "account_id": "12345",
  "total_domains": 2,
  "total_annual_renewal_cost": 30.00,
  "domains": [
    {
      "domain": "example.com",
      "expires_at": "2026-12-31",
      "auto_renew": true,
      "renewal_price": 15.00,
      "currency": "USD"
    },
    {
      "domain": "test.org",
      "expires_at": "2026-06-15",
      "auto_renew": false,
      "renewal_price": 15.00,
      "currency": "USD"
    }
  ]
}
```

### `list_dns_records`

List DNS records for a domain.

**Parameters:**
- `domain_name` (required): Domain name (e.g., "example.com")
- `name` (optional): Filter by record name
- `type` (optional): Filter by record type (A, AAAA, CNAME, MX, TXT, NS, SRV, ALIAS)

**Returns:**
- `domain`: Domain name
- `count`: Number of records
- `records`: Array of DNS record objects

**Example:**
```json
{
  "domain_name": "example.com",
  "type": "A"
}
```

**Response:**
```json
{
  "domain": "example.com",
  "count": 2,
  "records": [
    {
      "id": 123456,
      "name": "www",
      "type": "A",
      "content": "192.0.2.1",
      "ttl": 3600,
      "priority": null
    },
    {
      "id": 123457,
      "name": "@",
      "type": "A",
      "content": "192.0.2.1",
      "ttl": 3600,
      "priority": null
    }
  ]
}
```

### `configure_dns_record`

Create or update a DNS record. If a record with the same name and type exists, it will be updated.

**Parameters:**
- `domain_name` (required): Domain name (e.g., "example.com")
- `name` (required): Record name (e.g., "www" or "@" for root domain)
- `type` (required): DNS record type (A, AAAA, CNAME, MX, TXT, NS, SRV, ALIAS)
- `content` (required): Record content (IP address for A/AAAA, hostname for CNAME, etc.)
- `ttl` (optional): TTL in seconds (default: 3600)
- `priority` (optional): Priority for MX records

**Returns:**
- `success`: Boolean indicating success
- `action`: "created" or "updated"
- `record`: DNS record object

**Example (Create A record):**
```json
{
  "domain_name": "example.com",
  "name": "www",
  "type": "A",
  "content": "192.0.2.1",
  "ttl": 3600
}
```

**Example (Create MX record):**
```json
{
  "domain_name": "example.com",
  "name": "@",
  "type": "MX",
  "content": "mail.example.com",
  "priority": 10,
  "ttl": 3600
}
```

**Example (Create CNAME record):**
```json
{
  "domain_name": "example.com",
  "name": "blog",
  "type": "CNAME",
  "content": "example.github.io",
  "ttl": 3600
}
```

### `delete_dns_record`

Delete a DNS record by ID.

**Parameters:**
- `domain_name` (required): Domain name (e.g., "example.com")
- `record_id` (required): DNS record ID to delete

**Returns:**
- `success`: Boolean indicating success
- `message`: Success message

**Example:**
```json
{
  "domain_name": "example.com",
  "record_id": "123456"
}
```

### `disable_autorenew`

Disable auto-renewal for one or more domains.

**Parameters:**
- `domain_names` (required): Array of domain names to disable auto-renewal for

**Returns:**
- `results`: Array of operation results

**Example:**
```json
{
  "domain_names": ["example.com", "test.org"]
}
```

**Response:**
```json
{
  "results": [
    {
      "domain": "example.com",
      "status": "disabled",
      "error": null
    },
    {
      "domain": "test.org",
      "status": "disabled",
      "error": null
    }
  ]
}
```

### `transfer_domain`

Initiate a domain transfer to DNSimple. Requires authorization code from current registrar.

**Parameters:**
- `domain_name` (required): Domain name to transfer
- `auth_code` (required): Authorization code (EPP code) from current registrar
- `registrant_id` (optional): Registrant ID (uses account default if not provided)

**Returns:**
- `success`: Boolean indicating success
- `transfer`: Transfer object with ID and status

**Example:**
```json
{
  "domain_name": "example.com",
  "auth_code": "ABC123XYZ789"
}
```

**Response:**
```json
{
  "success": true,
  "transfer": {
    "id": 123456,
    "domain_id": 789012,
    "state": "new",
    "auto_renew": false,
    "whois_privacy": false
  }
}
```

**Note:** Domain must be unlocked at current registrar before transfer can be initiated.

## Error Handling

The server returns structured error messages in JSON format when operations fail. Common errors include:

- **Authentication errors**: Missing or invalid API token
- **Account errors**: No account found or account access issues
- **Domain errors**: Domain not found or not accessible
- **API errors**: DNSimple API errors with status codes and messages

**Example error response:**
```json
{
  "error": "Failed to get account ID: Invalid API response: missing 'data' key"
}
```

## Security Notes

- API tokens are never logged or exposed in error messages
- Tokens can be stored in environment variables or 1Password
- All API requests use HTTPS
- Token is automatically cached to `.env` file (with restricted permissions)

## Troubleshooting

1. **Authentication Fails**
   - Verify API token is correct
   - Check token has not expired
   - Ensure 1Password integration is configured correctly
   - Get new token from: https://dnsimple.com/user

2. **Account Not Found**
   - Verify account exists in DNSimple
   - Check account permissions
   - Ensure API token has account access

3. **Domain Not Found**
   - Verify domain exists in DNSimple account
   - Check domain spelling
   - Ensure domain is registered through DNSimple (for registrar operations)

4. **DNS Record Operations Fail**
   - Verify domain is using DNSimple nameservers
   - Check record name and type are valid
   - Ensure TTL is within valid range (60-604800 seconds)

## Notes

- The server automatically discovers the account ID from the DNSimple API
- DNS record updates will replace existing records with the same name and type
- Domain transfers require the domain to be unlocked at the current registrar
- All date fields are returned as ISO format strings
- The server runs in stdio mode for MCP communication

## License

MIT

## Support

- [GitHub Issues](https://github.com/markmhendrickson/mcp-server-dnsimple/issues)

