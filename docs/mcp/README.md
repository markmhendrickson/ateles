# MCP Servers

This directory contains all MCP (Model Context Protocol) servers for the repository. MCP servers provide standardized interfaces for AI assistants to interact with external systems and data sources.

## Available Servers

### Data Access Servers

- **`parquet/`** - Parquet Data Server
  - Primary data access interface for the Truth Layer
  - Read/write operations on all 60+ data types in `$DATA_DIR/`
  - Documentation: See `parquet/README.md`

### External API Integration Servers

- **`dnsimple/`** - DNSimple Server
  - Domain management, DNS configuration, pricing queries, domain transfers
  - Documentation: See `dnsimple/README.md`

- **`gmail/`** - Gmail Server
  - Gmail operations (read, send, search, filter management)
  - Documentation: See `gmail/README.md`

- **`google-calendar/`** - Google Calendar Server
  - Google Calendar integration (events, availability, multi-account support)
  - Documentation: See `google-calendar/README.md`

- **`google-analytics/`** - Google Analytics MCP (submodule)
  - GA4 read-only: account/property info, core and realtime reports
  - Official upstream: [googleanalytics/google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp). Setup: See `google-analytics/SETUP.md`

- **`instagram/`** - Instagram Server
  - Instagram Business accounts (profile management, media publishing, analytics, DMs)
  - Documentation: See `instagram/README.md`

- **`minted/`** - Minted.com Server
  - Minted.com API (contacts, orders, deliveries)
  - Documentation: See `minted/README.md`

- **`asana/`** - Asana Server
  - Asana integration (bidirectional sync, import, export, comments, metadata)
  - Documentation: See `asana/README.md`

- **`whatsapp/`** - WhatsApp Server
  - WhatsApp Business Platform API (messages, conversations)
  - Documentation: See `whatsapp/README.md`

- **`homekit/`** - HomeKit Server
  - HomeKit device control via HTTP API (Home Assistant, Homebridge, or native HomeKit)
  - Documentation: See `homekit/README.md`

## Remote Access

All MCP servers can be exposed remotely with authentication using the generic authenticated proxy:

```bash
# Quick start for any server
./execution/scripts/setup_mcp_server_tunnel.sh \
    <server-name> \
    <server-script-path> \
    [port] \
    [auth-token]
```

**Documentation:** See `REMOTE_ACCESS.md` for complete guide.

**Quick Reference:** See `QUICK_START_REMOTE.md` for server-specific examples.

## Configuration

### Cursor MCP Configuration

Add all servers to your Cursor MCP settings (`~/.cursor/mcp.json` or Cursor settings):

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {}
    },
    "dnsimple": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/dnsimple/dnsimple_mcp_server.py"
      ],
      "env": {
        "DNSIMPLE_API_TOKEN": "your-token-here"
      }
    },
    "gmail": {
      "command": "$REPO_ROOT/mcp/gmail/run-gmail-mcp.sh",
      "env": {
        "GOOGLE_OAUTH_CREDENTIALS": "${HOME}/.gmail-mcp/gcp-oauth.keys.json"
      }
    },
    "google-calendar": {
      "command": "$REPO_ROOT/mcp/google-calendar/run-google-calendar-mcp.sh",
      "env": {
        "GOOGLE_OAUTH_CREDENTIALS": "${HOME}/.google-calendar-mcp/gcp-oauth.keys.json"
      }
    },
    "instagram": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/instagram/src/instagram_mcp_server.py"
      ],
      "env": {}
    },
    "minted": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/minted/minted_mcp_server.py"
      ],
      "env": {}
    },
    "asana": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/asana/asana_mcp_server.py"
      ],
      "env": {
        "ASANA_SOURCE_PAT": "your-source-pat",
        "SOURCE_WORKSPACE_GID": "source-workspace-gid",
        "TARGET_WORKSPACE_GID": "target-workspace-gid"
      }
    },
    "whatsapp": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/whatsapp/whatsapp_mcp_server.py"
      ],
      "env": {}
    },
    "homekit": {
      "command": "python",
      "args": [
        "$REPO_ROOT/mcp/homekit/homekit_mcp_server.py"
      ],
      "env": {
        "HOMEKIT_API_URL": "http://homeassistant.local:8123/api",
        "HOMEKIT_API_TOKEN": "your-token-here",
        "HOMEKIT_BRIDGE_TYPE": "homeassistant"
      }
    }
  }
}
```

See `mcp-config-template.json` for a complete configuration template.

## Installation

Each server has its own requirements. See individual README files:

- **Parquet:** `cd mcp/parquet && pip install -r requirements.txt`
- **DNSimple:** `cd mcp/dnsimple && pip install -r requirements.txt`
- **Gmail:** `cd mcp/gmail && npm install`
- **Google Calendar:** `cd mcp/google-calendar && npm install && npm run build`
- **Instagram:** `cd mcp/instagram && pip install -r requirements.txt`
- **Minted:** `cd mcp/minted && pip install -r requirements.txt`
- **Asana:** `cd mcp/asana && pip install -r requirements.txt`
- **WhatsApp:** `cd mcp/whatsapp && pip install -r requirements.txt`
- **HomeKit:** `cd mcp/homekit && pip install -r requirements.txt`

## MCP Capabilities

MCP servers provide three core capabilities:

### Tools
Actions that can be executed (create, read, update, delete operations). All servers implement tools.

### Resources
Read-only, discoverable data/templates that AI assistants can fetch on demand. Resources provide context without requiring tool calls.

**Servers with Resources:**
- **Instagram** (`instagram/`): Profile, media, insights, pages
- **Parquet** (`parquet/`): Planned - tool schemas, data catalog, query patterns

**Recommended for all servers:**
- Tool schemas (`{server}://tools/{tool_name}`)
- Account/profile information
- Configuration templates
- Domain-specific lists (labels, calendars, devices, etc.)

See `docs/mcp_server_resource_audit.md` for complete resource audit and recommendations.

### Prompts
Formatted messages that provide guidance and workflows to AI assistants.

## Architecture

MCP servers span both the Truth Layer and Execution Layer:

- **Data Access**: Servers like `parquet/` provide access to the Truth Layer data substrate
- **External Actions**: Servers like `gmail/`, `dnsimple/`, etc. perform actions on external systems
- **Hybrid**: Many servers both access data and perform external actions

By consolidating all servers in `mcp/`, we avoid artificial distinctions and allow servers to naturally span both layers as needed.

## Notes

- All MCP servers use stdio transport by default (local only)
- Remote access is available via authenticated proxy (see `REMOTE_ACCESS.md`)
- All servers use MCP standard `Authorization: Bearer <token>` authentication for remote access
- See individual server README files for detailed documentation
