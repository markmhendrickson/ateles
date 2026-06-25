# Google Search Console MCP Server

A Model Context Protocol (MCP) server that provides Google Search Console integration for AI assistants like Claude.

## Features

- **List Sites**: List all sites/properties in your Search Console account
- **Get Search Analytics**: Retrieve search performance data (impressions, clicks, CTR, position)
- **Inspect URLs**: Check individual URL indexing status
- **Manage Sitemaps**: List and submit sitemaps
- **OAuth2 Authentication**: Automatic authentication with Google Cloud Platform
- **Credential Management**: Secure credential storage with file watching

## Installation

### Prerequisites

1. Node.js >= 14.0.0
2. A Google Cloud project with the Search Console API enabled
3. OAuth 2.0 credentials (Desktop app type)

### Google Cloud Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project or select an existing one
3. Enable the [Google Search Console API](https://console.cloud.google.com/apis/library/searchconsole.googleapis.com)
4. Create OAuth 2.0 credentials:
   - Go to **Credentials** → **Create Credentials** → **OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file and save it as `gcp-oauth.keys.json`

### Installation Steps

1. **Install dependencies:**
   ```bash
   cd mcp/google-search-console
   npm install
   ```

2. **Build the server:**
   ```bash
   npm run build
   ```

3. **Set up credentials:**
   - Place `gcp-oauth.keys.json` in the repository `.creds/` directory
   - Or set `SEARCH_CONSOLE_OAUTH_PATH` environment variable

4. **Authenticate:**
   ```bash
   npm run auth
   # Or
   node dist/index.js auth
   ```
   
   This will:
   - Open your browser for OAuth authentication
   - Save credentials to `.creds/search-console-credentials.json`

## Configuration

### Environment Variables

- `SEARCH_CONSOLE_OAUTH_PATH`: Path to OAuth keys file (default: `~/.search-console-mcp/gcp-oauth.keys.json`)
- `SEARCH_CONSOLE_CREDENTIALS_PATH`: Path to credentials file (default: `~/.search-console-mcp/credentials.json`)

### MCP Configuration

Add to your MCP configuration (e.g., `~/.cursor/mcp.json` or Cursor settings):

```json
{
  "mcpServers": {
    "google-search-console": {
      "command": "/path/to/repo/mcp/google-search-console/run-google-search-console-mcp.sh",
      "args": [],
      "env": {
        "SEARCH_CONSOLE_OAUTH_PATH": "/path/to/repo/.creds/gcp-oauth.keys.json",
        "SEARCH_CONSOLE_CREDENTIALS_PATH": "/path/to/repo/.creds/search-console-credentials.json"
      }
    }
  }
}
```

## Available Tools

### `list_sites`

Lists all sites/properties in your Search Console account.

**Example:**
```json
{}
```

**Response:**
```json
{
  "sites": [
    {
      "siteUrl": "sc-domain:example.com",
      "permissionLevel": "owner"
    }
  ]
}
```

### `get_search_analytics`

Gets search analytics data for a site.

**Parameters:**
- `siteUrl` (required): Site URL (e.g., `sc-domain:example.com` or `https://example.com/`)
- `startDate` (required): Start date in YYYY-MM-DD format
- `endDate` (required): End date in YYYY-MM-DD format
- `dimensions` (optional): Array of dimensions to group by (`query`, `page`, `country`, `device`, `searchAppearance`)
- `rowLimit` (optional): Maximum number of rows (default: 1000)

**Example:**
```json
{
  "siteUrl": "sc-domain:example.com",
  "startDate": "2026-01-01",
  "endDate": "2026-01-13",
  "dimensions": ["query", "page"],
  "rowLimit": 100
}
```

### `inspect_url`

Inspects a URL's indexing status in Google Search Console.

**Parameters:**
- `siteUrl` (required): Site URL
- `inspectionUrl` (required): URL to inspect

**Example:**
```json
{
  "siteUrl": "sc-domain:example.com",
  "inspectionUrl": "https://example.com/page"
}
```

### `list_sitemaps`

Lists all sitemaps submitted for a site.

**Parameters:**
- `siteUrl` (required): Site URL

**Example:**
```json
{
  "siteUrl": "sc-domain:example.com"
}
```

### `submit_sitemap`

Submits a sitemap to Google Search Console.

**Parameters:**
- `siteUrl` (required): Site URL
- `feedpath` (required): Sitemap path (e.g., `sitemap.xml`)

**Example:**
```json
{
  "siteUrl": "sc-domain:example.com",
  "feedpath": "sitemap.xml"
}
```

## Site URL Format

Google Search Console uses different URL formats:

- **Domain property**: `sc-domain:example.com` (for domain-level verification)
- **URL prefix property**: `https://example.com/` (for URL prefix verification)

Use the format that matches your Search Console property type.

## Limitations

### Indexing Issues API

The Search Console API does **not** provide a direct endpoint for bulk indexing issue retrieval. Indexing issues (404s, redirects, canonical issues, noindex tags) are available in the Search Console web interface but not via the API.

**Workarounds:**
- Use the `inspect_url` tool to check individual URLs
- Access the Search Console web interface for bulk issue reports
- Monitor Gmail for Search Console email notifications (use Gmail MCP server)

## Troubleshooting

### Authentication Issues

If authentication fails:
1. Verify OAuth credentials are correct
2. Ensure Search Console API is enabled in Google Cloud Console
3. Check that OAuth scopes include `webmasters.readonly` and `webmasters`
4. Delete credentials file and re-authenticate: `rm .creds/search-console-credentials.json && npm run auth`

### API Errors

Common API errors:
- **403 Forbidden**: Check API is enabled and credentials have correct permissions
- **404 Not Found**: Verify site URL format matches your property type
- **429 Too Many Requests**: Rate limit exceeded, wait before retrying

### Build Issues

If build fails:
```bash
# Clean and rebuild
rm -rf node_modules dist
npm install
npm run build
```

## Security

- Credentials are stored locally in `.creds/` directory
- Never commit credentials to version control
- Use environment variables for credential paths in production
- Credentials file is watched for changes and reloaded automatically

## Development

### Project Structure

```
google-search-console/
├── src/
│   └── index.ts          # Main server implementation
├── dist/                 # Compiled JavaScript (generated)
├── package.json
├── tsconfig.json
├── run-google-search-console-mcp.sh
└── README.md
```

### Building

```bash
npm run build
```

### Running

```bash
npm start
# Or
node dist/index.js
```

## Related

- [Google Search Console API Documentation](https://developers.google.com/webmaster-tools/search-console-api-original)
- [MCP SDK Documentation](https://modelcontextprotocol.io)
- [Gmail MCP Server](../gmail/README.md) - For monitoring Search Console email notifications

## License

ISC
