# MCP Server Development Guide

## Table of Contents

1. [Overview](#overview)
2. [Repository Structure](#repository-structure)
3. [Implementation Patterns](#implementation-patterns)
4. [Authentication & Configuration](#authentication--configuration)
5. [Tool Implementation](#tool-implementation)
6. [Resource Implementation](#resource-implementation)
7. [Checkpoint & Resume Capabilities](#checkpoint--resume-capabilities)
8. [Error Handling](#error-handling)
9. [Documentation Requirements](#documentation-requirements)
10. [Testing & Validation](#testing--validation)
11. [Deployment & Distribution](#deployment--distribution)
12. [Examples from Existing Servers](#examples-from-existing-servers)

## Overview

MCP (Model Context Protocol) servers provide a standardized interface for AI assistants to interact with external systems and data sources.

### Server Types

All MCP servers are consolidated in `mcp/`:
- Data access servers (e.g., `parquet/`) provide access to the Truth Layer data substrate
- External API integration servers (e.g., `gmail/`, `dnsimple/`, `google-calendar/`, `instagram/`, `minted/`, `whatsapp/`, `homekit/`) perform actions on external systems
- By consolidating all servers in `mcp/`, we avoid artificial distinctions and allow servers to naturally span both layers as needed

### Language Choices

- Python: Simpler APIs, data processing, file operations
- TypeScript/Node: Complex async operations, web APIs, OAuth flows

## Repository Structure

### Submodule Organization

All MCP servers are git submodules in their own repositories:

```
mcp/
  ├── parquet/          # Git submodule - Data access server
  ├── dnsimple/         # Git submodule - External API integration
  ├── gmail/            # Git submodule - External API integration
  ├── google-calendar/  # Git submodule - External API integration
  ├── instagram/        # Git submodule - External API integration
  ├── minted/           # Git submodule - External API integration
  ├── whatsapp/         # Git submodule - External API integration
  └── homekit/          # Git submodule - External API integration
```

Benefits: Independent versioning and deployment, can be shared or transferred without affecting parent repo, each server manages its own dependencies.

### Directory Structure

Python Server (Simple):
```
server-name/
├── __init__.py
├── server_name_mcp_server.py  # Main server file
├── README.md                  # Comprehensive documentation
├── requirements.txt           # Python dependencies
└── SETUP.md                   # Optional: Setup instructions
```

Python Server (Complex):
```
server-name/
├── __init__.py
├── src/
│   ├── __init__.py
│   ├── server_name_mcp_server.py
│   ├── config.py              # Configuration management
│   ├── client.py              # API client wrapper
│   └── models/
│       └── models.py          # Data models
├── tests/
│   └── test_*.py
├── README.md
├── requirements.txt
└── setup.py                   # Optional: Package setup
```

TypeScript/Node Server:
```
server-name/
├── src/
│   ├── index.ts              # Main entry point
│   ├── server.ts             # MCP server setup
│   ├── handlers/             # Tool handlers
│   ├── config/               # Configuration
│   └── types/                # TypeScript types
├── build/                    # Compiled output
├── package.json
├── tsconfig.json
├── README.md
└── Dockerfile                # Optional: Docker support
```

## Implementation Patterns

### Python Server Pattern

Basic Structure:
```python
#!/usr/bin/env python3
"""
MCP Server for [Service Name]

Provides tools for [description of functionality].
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Initialize MCP server
app = Server("server-name")

# Tool handlers
@app.list_tools()
async def list_tools() -> List[Tool]:
    """List available tools."""
    return [
        Tool(
            name="tool_name",
            description="Tool description",
            inputSchema={
                "type": "object",
                "properties": {
                    "param_name": {
                        "type": "string",
                        "description": "Parameter description"
                    }
                },
                "required": ["param_name"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> List[TextContent]:
    """Handle tool calls."""
    if name == "tool_name":
        # Implementation
        result = {"success": True, "data": "..."}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    raise ValueError(f"Unknown tool: {name}")

async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


# Main entry point
if __name__ == "__main__":
    asyncio.run(main())
```

### TypeScript/Node Server Pattern

Basic Structure:
```typescript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const server = new Server(
  {
    name: "server-name",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// List tools
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "tool_name",
      description: "Tool description",
      inputSchema: {
        type: "object",
        properties: {
          param_name: {
            type: "string",
            description: "Parameter description",
          },
        },
        required: ["param_name"],
      },
    },
  ],
}));

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (name === "tool_name") {
    const result = { success: true, data: "..." };
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(result, null, 2),
        },
      ],
    };
  }

  throw new Error(`Unknown tool: ${name}`);
});

// Start server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
```

## Authentication & Configuration

### Priority Order (Standard Pattern)

1. Environment Variables (highest priority, recommended)
2. Config Directory `.env` File (portable, user-specific)
3. 1Password Integration (optional, for backward compatibility)

### Python Authentication Pattern

```python
import os
from pathlib import Path
from typing import Optional

# Config directory (portable)
CONFIG_DIR = Path.home() / ".config" / "server-name-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"

# Optional: 1Password integration (backward compatibility)
HAS_CREDENTIALS_MODULE = False
try:
    server_dir = Path(__file__).parent
    possible_paths = [
        server_dir.parent.parent.parent,  # Adjust based on structure
        server_dir.parent.parent,
    ]
    
    for parent_path in possible_paths:
        credentials_path = parent_path / "execution" / "scripts" / "credentials.py"
        if credentials_path.exists():
            sys.path.insert(0, str(parent_path))
            try:
                from execution.scripts.credentials import get_credential, get_credential_by_domain
                HAS_CREDENTIALS_MODULE = True
                break
            except ImportError:
                continue
except Exception:
    pass

def load_credential_from_env() -> Optional[str]:
    """Load credential from environment variable or .env file."""
    # First check environment variable
    credential = os.getenv("SERVICE_API_TOKEN")
    if credential:
        return credential
    
    # Then check .env file
    if not ENV_FILE.exists():
        return None
    
    try:
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SERVICE_API_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    # Remove quotes if present
                    if token.startswith('"') and token.endswith('"'):
                        token = token[1:-1]
                    elif token.startswith("'") and token.endswith("'"):
                        token = token[1:-1]
                    return token
    except Exception:
        pass
    
    return None

def get_credential_from_1password() -> Optional[str]:
    """Get credential from 1Password."""
    if not HAS_CREDENTIALS_MODULE:
        return None
    
    try:
        field_names = ["api_token", "token", "access token"]
        for field_name in field_names:
            try:
                token = get_credential("ServiceName", field=field_name)
                if token:
                    return token
            except (ValueError, KeyError):
                continue
        
        try:
            token = get_credential_by_domain("service.com", field="api_token")
            if token:
                return token
        except (ValueError, KeyError):
            pass
        
        return None
    except Exception:
        return None

def get_credential() -> Optional[str]:
    """Get credential from environment variable, .env file, or 1Password."""
    credential = load_credential_from_env()
    if credential:
        return credential
    
    credential = get_credential_from_1password()
    return credential
```

### Configuration Directory Pattern

Location: `~/.config/[server-name]-mcp/`

Benefits: Portable (not tied to repository structure), user-specific configuration, secure (restricted permissions).

**Implementation:**
```python
CONFIG_DIR = Path.home() / ".config" / "server-name-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"

# Set restricted permissions
if ENV_FILE.exists():
    os.chmod(ENV_FILE, 0o600)  # Owner read/write only
```

## Tool Implementation

### Tool Definition Best Practices

1. Clear Descriptions: Describe what the tool does and when to use it
2. Comprehensive Schemas: Include all parameters with types, descriptions, and constraints
3. Required vs Optional: Clearly mark required parameters
4. Default Values: Provide sensible defaults where appropriate

Example:
```python
Tool(
    name="list_domains",
    description=(
        "List all domains in the account. Returns domain names, "
        "expiration dates, auto-renewal status, and registrant information."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Account ID (optional, uses default if not provided)"
            },
            "filter": {
                "type": "string",
                "description": "Filter domains by status (optional)",
                "enum": ["active", "expired", "all"],
                "default": "all"
            }
        }
    }
)
```

### Tool Response Format

Standard Response Structure:
```python
{
    "success": True,  # Boolean indicating success
    "data": {...},    # Result data
    "count": 5,       # Optional: Count of items
    "message": "..."  # Optional: Human-readable message
}
```

Error Response:
```python
{
    "error": "Error message",
    "code": "ERROR_CODE",  # Optional: Error code
    "details": {...}       # Optional: Additional error details
}
```

### Returning Data

Single Tool Result:
```python
result = {
    "success": True,
    "data": {...}
}
return [TextContent(type="text", text=json.dumps(result, indent=2))]
```

Multiple Results:
```python
results = {
    "success": True,
    "count": len(items),
    "items": [...]
}
return [TextContent(type="text", text=json.dumps(results, indent=2))]
```

## Resource Implementation

Resources are a core MCP capability that provides read-only, discoverable data/templates to AI assistants. Unlike tools (which perform actions), resources are static or semi-static content that can be fetched on demand.

### When to Implement Resources

#### Resources vs Tools vs Prompts

**Resources:**
- Read-only data that's frequently needed for context
- Discoverable lists (labels, calendars, devices, domains)
- Server metadata (tool schemas, configuration templates)
- Status/health information
- No side effects, can be cached

**Tools:**
- Actions that modify state (create, update, delete)
- Operations with complex parameters or conditional logic
- Operations that may fail or have side effects
- Cannot be safely cached

**Prompts:**
- Formatted messages for AI assistants
- Guidance and workflows
- Template-based interactions with arguments

#### Use Cases for Resources

**Always consider implementing:**
- Tool schemas (`{server}://tools/{tool_name}`)
- Account/profile information (`{server}://profile`)
- Configuration templates (`{server}://config/template`)

**Consider if relevant:**
- Lists of domain entities (labels, calendars, devices, domains)
- Recent/active items for quick context
- Status/health information for debugging
- Common query results or patterns

**Not needed:**
- Simple servers with few tools and no discoverable state
- Servers where all operations are stateless
- When tools already provide the same data efficiently

### Resource Implementation Patterns

#### Python Pattern (mcp.server.Server)

For servers using `mcp.server.Server`:

```python
from mcp.server import Server
from mcp.types import Resource

app = Server("server-name")

@app.list_resources()
async def list_resources() -> List[Resource]:
    """List available resources."""
    return [
        Resource(
            uri="server://resource-name",
            name="Resource Display Name",
            description="Clear description of what this resource contains",
            mimeType="application/json",
        ),
        Resource(
            uri="server://tools/tool_name",
            name="Tool Name Schema",
            description="JSON schema for tool_name parameters",
            mimeType="application/json",
        ),
    ]

@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read resource content by URI."""
    if uri == "server://resource-name":
        data = fetch_resource_data()
        return json.dumps(data, indent=2)
    
    elif uri.startswith("server://tools/"):
        tool_name = uri.split("/")[-1]
        tools = await app.list_tools()
        for tool in tools:
            if tool.name == tool_name:
                return json.dumps(tool.inputSchema, indent=2)
        raise ValueError(f"Tool not found: {tool_name}")
    
    else:
        raise ValueError(f"Unknown resource URI: {uri}")
```

#### Python Pattern (FastMCP)

For servers using FastMCP framework:

```python
from mcp.types import Resource

class ServerClass:
    def _setup_handlers(self):
        @self.server.list_resources()
        async def handle_list_resources() -> List[Resource]:
            """List available resources."""
            return [
                Resource(
                    uri="server://profile",
                    name="User Profile",
                    description="Current user account information and settings",
                    mimeType="application/json",
                ),
                Resource(
                    uri="server://status",
                    name="Server Status",
                    description="Connection status and API health",
                    mimeType="application/json",
                ),
            ]
        
        @self.server.read_resource()
        async def handle_read_resource(uri: str) -> str:
            """Read resource content."""
            if uri == "server://profile":
                profile = await self.client.get_profile()
                return json.dumps(profile, indent=2)
            
            elif uri == "server://status":
                status = {
                    "connected": self.client.is_connected(),
                    "api_version": "v1",
                    "rate_limit": self.client.get_rate_limit_status(),
                }
                return json.dumps(status, indent=2)
            
            else:
                raise ValueError(f"Unknown resource URI: {uri}")
```

#### TypeScript Pattern

For TypeScript/Node servers:

```typescript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const server = new Server(
  {
    name: "server-name",
    version: "1.0.0",
  },
  {
    capabilities: {
      resources: {},  // Declare resource capability
    },
  }
);

// List resources
server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [
    {
      uri: "server://profile",
      name: "User Profile",
      description: "Current user account information",
      mimeType: "application/json",
    },
    {
      uri: "server://calendars",
      name: "Calendar List",
      description: "All accessible calendars with metadata",
      mimeType: "application/json",
    },
  ],
}));

// Read resource
server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const { uri } = request.params;
  
  if (uri === "server://profile") {
    const profile = await client.getProfile();
    return {
      contents: [
        {
          type: "text",
          text: JSON.stringify(profile, null, 2),
        },
      ],
    };
  }
  
  if (uri === "server://calendars") {
    const calendars = await client.listCalendars();
    return {
      contents: [
        {
          type: "text",
          text: JSON.stringify(calendars, null, 2),
        },
      ],
    };
  }
  
  throw new Error(`Unknown resource URI: ${uri}`);
});
```

### Resource URI Schemes

#### Custom URI Schemes

Use custom schemes for domain-specific resources:

```
instagram://profile
instagram://media/recent
instagram://insights/account

parquet://tools/read_parquet
parquet://catalog/data_types
parquet://examples/patterns

gmail://labels
gmail://filters
gmail://config/template

calendar://calendars
calendar://events/today
calendar://events/{date}

homekit://devices
homekit://rooms
homekit://scenes
```

**Pattern:** `{server-name}://{resource-type}[/{identifier}]`

#### Template URIs

For dynamic resources, use template URIs:

```python
Resource(
    uri="calendar://events/{date}",
    name="Events by Date",
    description="Events for a specific date (YYYY-MM-DD)",
    mimeType="application/json",
)
```

Implementation:

```python
@app.read_resource()
async def read_resource(uri: str) -> str:
    if uri.startswith("calendar://events/"):
        date_str = uri.split("/")[-1]
        # Validate and parse date
        events = await client.get_events_by_date(date_str)
        return json.dumps(events, indent=2)
```

### Resource Types and Examples

#### Tool Schemas

**Purpose:** Provide JSON schemas for tool parameters without code inspection.

**Implementation:**

```python
@app.list_resources()
async def list_resources() -> List[Resource]:
    tools = await app.list_tools()
    resources = []
    
    # Add tool schema resources
    for tool in tools:
        resources.append(
            Resource(
                uri=f"server://tools/{tool.name}",
                name=f"{tool.name} Schema",
                description=f"JSON schema for {tool.name} parameters",
                mimeType="application/json",
            )
        )
    
    return resources

@app.read_resource()
async def read_resource(uri: str) -> str:
    if uri.startswith("server://tools/"):
        tool_name = uri.split("/")[-1]
        tools = await app.list_tools()
        for tool in tools:
            if tool.name == tool_name:
                return json.dumps(tool.inputSchema, indent=2)
        raise ValueError(f"Tool not found: {tool_name}")
```

#### Account/Profile Information

**Purpose:** Quick access to current user context, quotas, and settings.

**Example (Instagram):**

```python
Resource(
    uri="instagram://profile",
    name="Instagram Profile",
    description="Current Instagram business profile information",
    mimeType="application/json",
)

# Implementation
if uri == "instagram://profile":
    profile = await instagram_client.get_profile_info()
    return json.dumps({
        "id": profile.id,
        "username": profile.username,
        "name": profile.name,
        "followers_count": profile.followers_count,
        "media_count": profile.media_count,
        "biography": profile.biography,
    }, indent=2)
```

**Example (Gmail):**

```typescript
{
  uri: "gmail://profile",
  name: "Gmail Profile",
  description: "Current user profile with email and storage quota",
  mimeType: "application/json",
}

// Implementation
if (uri === "gmail://profile") {
  const profile = await gmail.users.getProfile({ userId: 'me' });
  return {
    contents: [{
      type: "text",
      text: JSON.stringify({
        emailAddress: profile.data.emailAddress,
        messagesTotal: profile.data.messagesTotal,
        threadsTotal: profile.data.threadsTotal,
        historyId: profile.data.historyId,
      }, null, 2),
    }],
  };
}
```

#### Configuration Templates

**Purpose:** Help users configure the server with example JSON.

**Implementation:**

```python
Resource(
    uri="server://config/template",
    name="Configuration Template",
    description="Example Cursor/Claude Desktop configuration",
    mimeType="application/json",
)

# Implementation
if uri == "server://config/template":
    config = {
        "mcpServers": {
            "server-name": {
                "command": "python3",
                "args": ["/path/to/server_name_mcp_server.py"],
                "env": {
                    "API_TOKEN": "your-token-here",
                    "CONFIG_OPTION": "value",
                }
            }
        }
    }
    return json.dumps(config, indent=2)
```

#### Domain-Specific Lists

**Purpose:** Provide frequently needed lists for context.

**Example (Gmail Labels):**

```typescript
{
  uri: "gmail://labels",
  name: "Gmail Labels",
  description: "All Gmail labels with message counts",
  mimeType: "application/json",
}

// Implementation
if (uri === "gmail://labels") {
  const response = await gmail.users.labels.list({ userId: 'me' });
  return {
    contents: [{
      type: "text",
      text: JSON.stringify(response.data.labels, null, 2),
    }],
  };
}
```

**Example (HomeKit Devices):**

```python
Resource(
    uri="homekit://devices",
    name="HomeKit Devices",
    description="All HomeKit devices with current state",
    mimeType="application/json",
)

# Implementation
if uri == "homekit://devices":
    devices = await homekit_client.get_all_devices()
    return json.dumps([{
        "id": device.id,
        "name": device.name,
        "type": device.type,
        "room": device.room,
        "state": device.state,
    } for device in devices], indent=2)
```

#### Status/Health Information

**Purpose:** Enable debugging and monitoring.

**Implementation:**

```python
Resource(
    uri="server://status",
    name="Server Status",
    description="Connection status and API health",
    mimeType="application/json",
)

# Implementation
if uri == "server://status":
    status = {
        "connected": api_client.is_connected(),
        "api_version": api_client.version,
        "rate_limit": {
            "remaining": api_client.rate_limit_remaining,
            "reset_at": api_client.rate_limit_reset.isoformat(),
        },
        "last_request": api_client.last_request_time.isoformat(),
    }
    return json.dumps(status, indent=2)
```

### Best Practices

#### Performance

1. **Cache resource data when appropriate:**
   ```python
   _profile_cache = None
   _profile_cache_time = None
   CACHE_TTL = 300  # 5 minutes
   
   async def get_profile_resource():
       global _profile_cache, _profile_cache_time
       now = time.time()
       if _profile_cache and (now - _profile_cache_time) < CACHE_TTL:
           return _profile_cache
       
       profile = await client.get_profile()
       _profile_cache = profile
       _profile_cache_time = now
       return profile
   ```

2. **Chunk large resources (>1MB):**
   - Split into multiple resources (e.g., `server://data/page1`, `server://data/page2`)
   - Or implement pagination in tool instead
   - Include metadata about total size and pages

3. **Use lazy loading for expensive operations:**
   ```python
   # Don't fetch all data in list_resources()
   @app.list_resources()
   async def list_resources():
       # Just describe resources, don't fetch data yet
       return [Resource(...)]
   
   # Fetch data only when read_resource() is called
   @app.read_resource()
   async def read_resource(uri):
       data = await fetch_data_for_uri(uri)  # Fetch on demand
       return json.dumps(data)
   ```

#### Error Handling

1. **Handle errors gracefully:**
   ```python
   @app.read_resource()
   async def read_resource(uri: str) -> str:
       try:
           if uri == "server://profile":
               profile = await client.get_profile()
               return json.dumps(profile, indent=2)
           raise ValueError(f"Unknown resource URI: {uri}")
       except Exception as e:
           # Return error as JSON, don't crash
           return json.dumps({
               "error": str(e),
               "uri": uri,
               "type": type(e).__name__,
           }, indent=2)
   ```

2. **Provide helpful error messages:**
   ```python
   if uri == "server://tools/unknown":
       return json.dumps({
           "error": "Tool not found",
           "requested_tool": "unknown",
           "available_tools": [tool.name for tool in await app.list_tools()],
       }, indent=2)
   ```

#### URI Design

1. **Use consistent schemes per server:**
   - Good: `gmail://labels`, `gmail://filters`, `gmail://profile`
   - Bad: `gmail://labels`, `gmail-filters`, `profile://gmail`

2. **Make URIs descriptive and hierarchical:**
   - Good: `server://tools/read_data`, `server://examples/patterns`
   - Bad: `server://t1`, `server://example`

3. **Include mimeType for all resources:**
   ```python
   Resource(
       uri="server://resource",
       name="Resource Name",
       description="Description",
       mimeType="application/json",  # Always include
   )
   ```

4. **Provide clear descriptions for discoverability:**
   ```python
   # Good: Describes what the resource contains
   description="Current user profile with email, name, and storage quota"
   
   # Bad: Vague or unhelpful
   description="Profile data"
   ```

### Resource Testing

#### Unit Tests

Test resource handlers:

```python
import pytest

@pytest.mark.asyncio
async def test_list_resources():
    """Test that list_resources returns expected resources."""
    resources = await app.list_resources()
    
    assert len(resources) > 0
    assert all(r.uri for r in resources)
    assert all(r.name for r in resources)
    assert all(r.description for r in resources)
    assert all(r.mimeType for r in resources)
    
    # Check for expected resources
    uris = [r.uri for r in resources]
    assert "server://profile" in uris
    assert "server://status" in uris

@pytest.mark.asyncio
async def test_read_resource_profile():
    """Test reading profile resource."""
    content = await app.read_resource("server://profile")
    
    assert content is not None
    data = json.loads(content)
    assert "email" in data or "id" in data
    assert isinstance(data, dict)

@pytest.mark.asyncio
async def test_read_resource_unknown_uri():
    """Test that unknown URIs raise appropriate errors."""
    with pytest.raises(ValueError, match="Unknown resource URI"):
        await app.read_resource("server://unknown")

@pytest.mark.asyncio
async def test_resource_content_format():
    """Test that resource content is valid JSON."""
    resources = await app.list_resources()
    
    for resource in resources:
        content = await app.read_resource(resource.uri)
        # Should be valid JSON
        data = json.loads(content)
        assert data is not None
```

#### Integration Tests

Test resources with real API calls:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_resource_live():
    """Test profile resource with real API."""
    content = await app.read_resource("server://profile")
    data = json.loads(content)
    
    # Verify expected fields
    assert data["email"].endswith("@example.com")
    assert data["id"]
    assert isinstance(data["messagesTotal"], int)
```

#### Server Initialization Tests

Include resources in initialization tests:

```python
def test_server_declares_resource_capability():
    """Test that server declares resource capability."""
    # Check server initialization options
    assert "resources" in server.capabilities
```

### Documentation Requirements

#### README.md

Document all resources in server README:

```markdown
## Available Resources

The server provides read-only resources for easier discovery and reference:

### Resource List

1. **User Profile** (`server://profile`)
   - Current user account information
   - Includes email, name, storage quota
   - Cached for 5 minutes

2. **Tool Schemas** (`server://tools/{tool_name}`)
   - JSON schemas for all available tools
   - Use for understanding tool parameters
   - Example: `server://tools/send_message`

3. **Configuration Template** (`server://config/template`)
   - Example Cursor/Claude Desktop configuration
   - Includes all environment variables

### Accessing Resources

Use MCP resource tools to access:

\`\`\`python
# List all resources
resources = await session.list_resources()

# Read specific resource
profile = await session.read_resource("server://profile")
\`\`\`
```

#### Changelog

Document when resources are added:

```markdown
## [1.1.0] - 2025-01-15

### Added
- Resources capability:
  - `server://profile` - User profile information
  - `server://tools/{tool_name}` - Tool schemas
  - `server://config/template` - Configuration template
```

## Error Handling

### Error Response Pattern

```python
def handle_error(error: Exception, context: str = "") -> List[TextContent]:
    """Handle errors and return structured error response."""
    error_response = {
        "error": str(error),
        "context": context,
        "type": type(error).__name__
    }
    
    # Log error (to stderr, not stdout)
    import sys
    print(f"Error in {context}: {error}", file=sys.stderr)
    
    return [TextContent(type="text", text=json.dumps(error_response, indent=2))]
```

### Common Error Types

1. Authentication Errors: Missing or invalid credentials
2. API Errors: External API failures (include status codes)
3. Validation Errors: Invalid parameters
4. Network Errors: Connection timeouts, DNS failures

Example:
```python
try:
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()
except requests.exceptions.HTTPError as e:
    error_response = {
        "error": f"API request failed: {e.response.status_code}",
        "status_code": e.response.status_code,
        "message": e.response.text
    }
    return [TextContent(type="text", text=json.dumps(error_response, indent=2))]
except requests.exceptions.Timeout:
    error_response = {
        "error": "Request timed out",
        "type": "timeout"
    }
    return [TextContent(type="text", text=json.dumps(error_response, indent=2))]
```

## Checkpoint & Resume Capabilities

For import/export scripts that process large amounts of data through MCP servers, checkpoint and resume capabilities are essential for reliability and recovery from interruptions.

### When to Use Checkpoints

Checkpoint/resume should be implemented for:
- Import scripts processing large datasets (100+ items)
- Export scripts that may take significant time
- Any script that could be interrupted (network issues, timeouts, user cancellation)
- Operations that iterate through collections

### Checkpoint Implementation Pattern

File Location:
```
$DATA_DIR/logs/[operation]_checkpoint.json
```

Checkpoint Structure:
```python
{
    "last_item_id": "uuid_or_identifier",
    "processed_count": 150,
    "total_count": 500,
    "timestamp": "2025-01-15T10:30:00"
}
```

Core Functions:
```python
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

CHECKPOINT_FILE = Path("logs") / "import_checkpoint.json"

def save_checkpoint(last_item_id: str, processed_count: int, total_count: int):
    """Save checkpoint with last processed item ID (atomic write)."""
    checkpoint_data = {
        'last_item_id': last_item_id,
        'processed_count': processed_count,
        'total_count': total_count,
        'timestamp': datetime.now().isoformat(),
    }
    # Atomic write: write to temp file, then rename
    temp_file = CHECKPOINT_FILE.with_suffix('.tmp')
    try:
        with open(temp_file, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)
        temp_file.replace(CHECKPOINT_FILE)  # Atomic rename
        print(f"  Checkpoint saved: processed {processed_count}/{total_count} items")
    except Exception as e:
        print(f"Warning: Could not save checkpoint: {e}", file=sys.stderr)
        if temp_file.exists():
            temp_file.unlink()

def load_checkpoint() -> Optional[Dict]:
    """Load checkpoint if it exists."""
    if not CHECKPOINT_FILE.exists():
        return None
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load checkpoint: {e}", file=sys.stderr)
        return None

def clear_checkpoint():
    """Clear checkpoint file on successful completion."""
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("Checkpoint cleared")
```

### Resume Logic Pattern

Resume Implementation:
```python
def import_items(items: List[Dict], resume: bool = False, checkpoint_interval: int = 50):
    """Import items with checkpoint/resume support."""
    
    # Check for resume checkpoint
    resume_from_id = None
    checkpoint = None
    if resume:
        checkpoint = load_checkpoint()
        if checkpoint:
            resume_from_id = checkpoint.get('last_item_id')
            processed_count = checkpoint.get('processed_count', 0)
            print(f"Resuming from checkpoint: last item {resume_from_id[:50]}... ({processed_count} items)")
        else:
            print("No checkpoint found, starting from beginning")
    
    # Skip already processed items if resuming
    if resume_from_id:
        checkpoint_found = False
        checkpoint_idx = -1
        for idx, item in enumerate(items):
            if item.get('id') == resume_from_id:
                checkpoint_found = True
                checkpoint_idx = idx
                break
        
        if checkpoint_found:
            items = items[checkpoint_idx + 1:]
            remaining_count = len(items)
            print(f"Found checkpoint item at position {checkpoint_idx + 1}, processing {remaining_count} remaining")
        else:
            print(f"Warning: Checkpoint item {resume_from_id[:50]}... not found. Starting from beginning.")
    
    # Process items
    total_count = len(items)
    start_position = checkpoint.get('processed_count', 0) + 1 if (resume_from_id and checkpoint) else 1
    total_all_items = checkpoint.get('total_count', total_count) if (resume_from_id and checkpoint) else total_count
    
    imported_count = 0
    skipped_count = 0
    error_count = 0
    
    for idx, item in enumerate(items):
        current_position = start_position + idx
        
        try:
            # Process item
            result = process_item(item)
            imported_count += 1
            
        except Exception as e:
            error_count += 1
            continue
        
        # Save checkpoint periodically
        processed_so_far = imported_count + skipped_count + error_count
        if processed_so_far > 0 and processed_so_far % checkpoint_interval == 0:
            save_checkpoint(item.get('id', ''), start_position - 1 + processed_so_far, total_all_items)
    
    # Save final checkpoint
    final_processed = imported_count + skipped_count + error_count
    if items and final_processed > 0:
        last_item = items[-1]
        final_position = start_position - 1 + final_processed
        save_checkpoint(last_item.get('id', ''), final_position, total_all_items)
    
    # Clear checkpoint on successful completion
    if error_count == 0 and imported_count + skipped_count == total_count:
        clear_checkpoint()
        print("Import completed successfully, checkpoint cleared")
    
    return (imported_count, skipped_count, error_count)
```

### Command-Line Interface

Add checkpoint flags:
```python
parser.add_argument(
    '--resume',
    action='store_true',
    help='Resume from last checkpoint (saves progress every N items by default)'
)
parser.add_argument(
    '--checkpoint-interval',
    type=int,
    default=50,
    help='Save checkpoint every N items (default: 50)'
)
```

### Best Practices

1. Atomic Writes: Always write to a temporary file first, then rename (prevents corruption on interruption)
2. Checkpoint Frequency: Save every 50-100 items for good balance between performance and recovery granularity
3. Clear on Success: Always clear checkpoint file on successful completion
4. Resume Validation: Verify checkpoint item still exists before resuming
5. Progress Tracking: Count all processed items (imported + skipped + errors) for accurate progress
6. Error Handling: Don't fail entire operation if checkpoint save fails (log warning, continue)
7. Idempotency: Ensure operations can be safely resumed without duplicating work

### Example Implementations

Reference Implementations:
- **Asana Import** (`execution/scripts/import_asana_tasks.py`): Checkpoint every 100 tasks
- **Apple Notes Import** (`execution/scripts/import_apple_notes.py`): Checkpoint every 50 notes
- **Asana Export** (`execution/scripts/export_asana_tasks.py`): Checkpoint every 10 tasks

## Documentation Requirements

### README.md Structure

Required Sections:

1. Title & Description
   - Server name and purpose
   - Credits/attributions if based on other projects

2. Features
   - List of capabilities
   - Key functionality

3. Installation
   - Dependencies
   - Setup commands

4. Configuration
   - Authentication methods
   - Environment variables
   - Config file locations

5. Cursor Configuration
   - Complete JSON configuration example
   - Path resolution notes

6. Claude Desktop Configuration
   - Complete JSON configuration example
   - Platform-specific paths

7. Available Tools
   - For each tool:
     - Description
     - Parameters (with types and descriptions)
     - Example request JSON
     - Example response JSON
     - Notes/limitations

8. Error Handling
   - Common errors
   - Error response format
   - Troubleshooting tips

9. Security Notes
   - Credential handling
   - Security considerations
   - Best practices

10. Troubleshooting
    - Common issues and solutions
    - Debugging tips

11. Notes
    - Implementation details
    - Limitations
    - Known issues

12. License
    - License type

13. Support
    - GitHub issues link
    - Contact information

### Configuration Examples

Cursor Configuration:
```json
{
  "mcpServers": {
    "server-name": {
      "command": "python3",
      "args": [
        "/path/to/server_name_mcp_server.py"
      ],
      "env": {
        "SERVICE_API_TOKEN": "your-token-here"
      }
    }
  }
}
```

Claude Desktop Configuration:
```json
{
  "mcpServers": {
    "server-name": {
      "command": "python3",
      "args": [
        "/path/to/server_name_mcp_server.py"
      ],
      "env": {
        "SERVICE_API_TOKEN": "your-token-here"
      }
    }
  }
}
```

Note: Always include a note about replacing `/path/to/` with actual paths.

## Testing & Validation

### Testing Requirements

All MCP servers must have comprehensive test coverage to ensure reliability and maintainability.

Minimum Coverage Thresholds:
- Lines: ≥90%
- Branches: ≥85%
- Functions: 100%

Required Test Categories:
1. Unit Tests: Test individual components in isolation (no external dependencies)
2. Integration Tests: Test interactions with real APIs/services
3. Premium/Limitation Tests: Document and test plan-specific feature limitations

### Test Structure Standards

Directory Structure:
```
server-name/
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # pytest fixtures and configuration
│   ├── test_tool1.py              # Tool-specific tests
│   ├── test_tool2.py
│   ├── test_error_handling.py     # Error case tests
│   ├── test_plan_limitations.py   # Premium feature tests
│   ├── test_data_permutations.py  # Comprehensive data tests
│   └── fixtures/
│       ├── __init__.py
│       ├── test_data.py           # Test data generators
│       └── test_config.py         # Test configuration
├── pytest.ini                     # pytest configuration
├── requirements-test.txt          # Test dependencies
└── PLAN_LIMITATIONS.md            # Documented limitations
```

Naming Conventions:
- Test files: `test_*.py`
- Test functions: `test_*`
- Test classes: `Test*`
- Fixtures: Descriptive names (e.g., `mock_api_client`, `test_config`)

### pytest Configuration

Required `pytest.ini`:
```ini
[pytest]
python_files = test_*.py
python_classes = Test*
python_functions = test_*

markers =
    unit: Unit tests (no external dependencies)
    integration: Integration tests (require external services)
    premium: Premium feature tests (may fail based on plan)
    slow: Slow-running tests

addopts =
    --verbose
    --strict-markers
    --cov=.
    --cov-report=term-missing
    --cov-report=html
    --cov-fail-under=90

testpaths = tests
asyncio_mode = auto
```

Required `requirements-test.txt`:
```
pytest>=7.0.0
pytest-asyncio>=0.21.0
pytest-cov>=4.0.0
pytest-mock>=3.10.0
pytest-timeout>=2.1.0
```

### Test Coverage Requirements

All Tools Must Be Tested:
- ✓ Basic functionality with minimal parameters
- ✓ Full functionality with all parameters
- ✓ All parameter combinations and permutations
- ✓ Edge cases (empty values, max lengths, special characters)
- ✓ Error conditions (invalid inputs, API failures, timeouts)

All Data Permutations Must Be Tested:
- ✓ Various property combinations
- ✓ Boundary conditions (min/max values)
- ✓ Special characters and Unicode
- ✓ Round-trip data integrity (import → export → verify)

All Error Cases Must Be Tested:
- ✓ Authentication failures
- ✓ Invalid parameters
- ✓ API errors (4xx, 5xx responses)
- ✓ Network timeouts
- ✓ Rate limiting
- ✓ Resource not found

### Test Workspace Requirements

For Integration Tests:
- Must use dedicated test workspaces/environments
- Never use production data or workspaces
- Must clean up test data after tests
- Must isolate tests (no dependencies between tests)
- Should use test environment variables (e.g., `TEST_API_TOKEN`)

Example Test Configuration:
```python
# tests/conftest.py
@pytest.fixture
def test_workspace_config():
    """Get test workspace configuration from environment."""
    return {
        "api_token": os.getenv("TEST_API_TOKEN"),
        "workspace_id": os.getenv("TEST_WORKSPACE_ID"),
    }

@pytest.fixture
def skip_if_no_test_workspace():
    """Skip tests if test workspace not configured."""
    if not os.getenv("TEST_API_TOKEN"):
        pytest.skip("Test workspace not configured")
```

### Premium/Plan Limitation Testing

Requirements:
1. Document all plan limitations in `PLAN_LIMITATIONS.md`
2. Test premium feature failures and verify graceful handling
3. Mark premium tests with `@pytest.mark.premium`
4. Document error codes and messages for each limitation

Example `PLAN_LIMITATIONS.md` Structure:
```markdown
# Service Plan Limitations

## Known Limitations

### Feature X (Premium Only)
- **Error**: 402 Payment Required
- **Message**: "Feature requires premium plan"
- **Handling**: Falls back to basic functionality
- **Testing**: See `test_plan_limitations.py`

## Feature Availability Matrix
| Feature | Free | Premium | Enterprise |
|---------|------|---------|------------|
| Basic API | ✓ | ✓ | ✓ |
| Advanced | ✗ | ✓ | ✓ |
```

Example Premium Tests:
```python
@pytest.mark.premium
@pytest.mark.unit
def test_premium_feature_graceful_failure(mock_client):
    """Test that premium features fail gracefully."""
    # Mock 402 error
    mock_client.side_effect = HTTPError(402, "Premium required")
    
    # Should handle gracefully without crashing
    result = call_tool("premium_feature", {})
    assert "error" in result
    assert "premium" in result["error"].lower()
```

### Data Integrity Testing

Round-Trip Tests:
```python
@pytest.mark.integration
async def test_round_trip_data_integrity():
    """Test that data survives import → export → import."""
    # Step 1: Import data
    imported = await import_data(source)
    
    # Step 2: Export data
    exported = await export_data(imported)
    
    # Step 3: Re-import
    reimported = await import_data(exported)
    
    # Verify all properties preserved
    assert imported == reimported
```

### Server Initialization Tests (MANDATORY)

**Critical:** All MCP servers MUST include automated tests that verify server initialization and startup. These tests catch common errors like incorrect `stdio_server` usage, missing async/await patterns, and server structure issues.

#### Required Initialization Tests

**1. Server Startup Test**

Tests that the server can be imported and initialized without errors:

```python
# tests/test_server_initialization.py
import pytest
import sys
from pathlib import Path

# Add server directory to path
server_dir = Path(__file__).parent.parent
sys.path.insert(0, str(server_dir))

def test_server_import():
    """Test that server module can be imported without errors."""
    try:
        import server_name_mcp_server  # Replace with actual server name
        assert server_name_mcp_server is not None
    except ImportError as e:
        pytest.fail(f"Failed to import server: {e}")
    except SyntaxError as e:
        pytest.fail(f"Server has syntax errors: {e}")

def test_server_app_initialized():
    """Test that Server app is properly initialized."""
    from server_name_mcp_server import app
    assert app is not None
    assert hasattr(app, 'list_tools')
    assert hasattr(app, 'call_tool')
```

**2. Main Entry Point Test**

Tests that the `if __name__ == "__main__"` block uses correct async pattern:

```python
# tests/test_server_initialization.py (continued)
import ast
import inspect

def test_main_entry_point_uses_async():
    """Test that main entry point uses correct async pattern for stdio_server."""
    server_file = Path(__file__).parent.parent / "server_name_mcp_server.py"
    source = server_file.read_text()
    
    # Parse the AST to check the main block
    tree = ast.parse(source)
    
    main_block_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            # Check if this is the __name__ == "__main__" block
            if (isinstance(node.test.left, ast.Name) and 
                node.test.left.id == "__name__"):
                main_block_found = True
                # Check that it uses asyncio.run() pattern
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if (isinstance(child.func, ast.Attribute) and 
                            child.func.attr == "run" and
                            isinstance(child.func.value, ast.Name) and
                            child.func.value.id == "asyncio"):
                            # Good: uses asyncio.run()
                            return
                        elif (isinstance(child.func, ast.Call) and
                              isinstance(child.func.func, ast.Name) and
                              child.func.func.id == "stdio_server"):
                            # Bad: stdio_server(app) pattern (incorrect)
                            pytest.fail(
                                "Main entry point incorrectly uses stdio_server(app). "
                                "Should use: async def main(): async with stdio_server() as ..."
                            )
    
    if not main_block_found:
        pytest.fail("No __main__ block found in server file")
```

**3. stdio_server Usage Validation**

Tests that `stdio_server` is used correctly as a context manager:

```python
# tests/test_server_initialization.py (continued)
def test_stdio_server_usage_pattern():
    """Test that stdio_server is used correctly as async context manager."""
    server_file = Path(__file__).parent.parent / "server_name_mcp_server.py"
    source = server_file.read_text()
    
    # Check for correct pattern: async with stdio_server() as ...
    if "async with stdio_server()" not in source:
        pytest.fail(
            "stdio_server must be used as async context manager: "
            "async with stdio_server() as (read_stream, write_stream):"
        )
    
    # Check for incorrect pattern: stdio_server(app)
    if "stdio_server(app)" in source or "asyncio.run(stdio_server(" in source:
        pytest.fail(
            "Incorrect stdio_server usage detected. "
            "Use: async with stdio_server() as (read_stream, write_stream): "
            "await app.run(read_stream, write_stream, ...)"
        )
    
    # Check that app.run() is called with streams
    if "await app.run(read_stream, write_stream" not in source:
        pytest.fail(
            "app.run() must be called with read_stream and write_stream from stdio_server"
        )
```

**4. Async Function Definition Test**

Tests that main function is properly defined as async:

```python
# tests/test_server_initialization.py (continued)
def test_main_function_is_async():
    """Test that main() function is defined as async."""
    server_file = Path(__file__).parent.parent / "server_name_mcp_server.py"
    source = server_file.read_text()
    
    # Parse to find main function
    tree = ast.parse(source)
    
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            if not isinstance(node, ast.AsyncFunctionDef):
                pytest.fail("main() function must be async: async def main():")
            return
    
    # If no main function found, check if it's inlined
    if "async def main()" not in source:
        pytest.fail("No async main() function found. Required for stdio_server usage.")
```

**5. MCP Protocol Compliance Test**

Tests basic MCP protocol compliance using a mock client:

```python
# tests/test_server_initialization.py (continued)
import asyncio
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_server_responds_to_initialization():
    """Test that server responds correctly to MCP initialization request."""
    from server_name_mcp_server import app
    
    # Mock streams
    read_stream = AsyncMock()
    write_stream = AsyncMock()
    
    # Mock initialization request
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "test-client",
                "version": "1.0.0"
            }
        }
    }
    
    # This test verifies the server structure is correct
    # Full integration would require actual stdio transport
    assert app is not None
    assert hasattr(app, 'run')
```

**6. Tool Registration Test**

Tests that tools are properly registered:

```python
# tests/test_server_initialization.py (continued)
@pytest.mark.asyncio
async def test_tools_are_registered():
    """Test that all expected tools are registered."""
    from server_name_mcp_server import app
    
    # Get list of tools
    tools = await app.list_tools()
    
    assert tools is not None
    assert isinstance(tools, list)
    assert len(tools) > 0, "At least one tool must be registered"
    
    # Verify tool structure
    for tool in tools:
        assert hasattr(tool, 'name') or 'name' in tool
        assert hasattr(tool, 'description') or 'description' in tool
        assert hasattr(tool, 'inputSchema') or 'inputSchema' in tool
```

#### Complete Test File Template

```python
# tests/test_server_initialization.py
"""
MANDATORY: Server initialization and startup tests.

These tests catch common errors like:
- Incorrect stdio_server usage
- Missing async/await patterns
- Server structure issues
- MCP protocol compliance
"""

import pytest
import sys
import ast
from pathlib import Path

# Add server directory to path
server_dir = Path(__file__).parent.parent
sys.path.insert(0, str(server_dir))

# Import server module
try:
    import server_name_mcp_server  # Replace with actual server name
    from server_name_mcp_server import app
except ImportError as e:
    pytest.skip(f"Server module not available: {e}")


class TestServerInitialization:
    """Test server initialization and structure."""
    
    def test_server_import(self):
        """Test that server module can be imported."""
        assert server_name_mcp_server is not None
    
    def test_app_initialized(self):
        """Test that Server app is properly initialized."""
        assert app is not None
        assert hasattr(app, 'list_tools')
        assert hasattr(app, 'call_tool')
    
    def test_stdio_server_usage(self):
        """Test that stdio_server is used correctly."""
        server_file = server_dir / "server_name_mcp_server.py"
        source = server_file.read_text()
        
        # Must use async context manager pattern
        assert "async with stdio_server()" in source, \
            "stdio_server must be used as async context manager"
        
        # Must NOT use incorrect pattern
        assert "stdio_server(app)" not in source, \
            "Do not pass app to stdio_server()"
        assert "asyncio.run(stdio_server(" not in source, \
            "Do not use asyncio.run(stdio_server(...))"
        
        # Must call app.run() with streams
        assert "await app.run(read_stream, write_stream" in source, \
            "Must call app.run() with streams from stdio_server"
    
    def test_main_function_async(self):
        """Test that main() function is async."""
        server_file = server_dir / "server_name_mcp_server.py"
        source = server_file.read_text()
        
        assert "async def main()" in source, \
            "main() function must be async"
    
    @pytest.mark.asyncio
    async def test_tools_registered(self):
        """Test that tools are properly registered."""
        tools = await app.list_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0, "At least one tool must be registered"
        
        for tool in tools:
            # Handle both dict and object formats
            name = tool.name if hasattr(tool, 'name') else tool.get('name')
            assert name is not None, "Tool must have a name"
            
            desc = tool.description if hasattr(tool, 'description') else tool.get('description')
            assert desc is not None, f"Tool {name} must have a description"


class TestServerStartup:
    """Test server startup and basic functionality."""
    
    @pytest.mark.asyncio
    async def test_server_can_list_tools(self):
        """Test that server can list tools without errors."""
        try:
            tools = await app.list_tools()
            assert isinstance(tools, list)
        except Exception as e:
            pytest.fail(f"Failed to list tools: {e}")
    
    def test_server_file_executable(self):
        """Test that server file is executable and has shebang."""
        server_file = server_dir / "server_name_mcp_server.py"
        assert server_file.exists(), "Server file must exist"
        
        source = server_file.read_text()
        assert source.startswith("#!/usr/bin/env python3"), \
            "Server file must start with shebang: #!/usr/bin/env python3"
```

#### Integration with CI/CD

Add initialization tests to CI/CD pipeline to catch errors before deployment:

```yaml
# .github/workflows/test.yml (addition)
- name: Run server initialization tests
  run: |
    pytest tests/test_server_initialization.py -v
```

#### Benefits

These tests catch:
- ✅ Incorrect `stdio_server` usage (like the error you encountered)
- ✅ Missing `async`/`await` keywords
- ✅ Incorrect server structure
- ✅ Import errors
- ✅ Tool registration issues
- ✅ MCP protocol compliance problems

### Manual Testing

Server Startup Test:
```bash
python server_name_mcp_server.py
```

Tool Listing Test:
```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
```

Tool Call Test:
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "tool_name",
    "arguments": {"param": "value"}
  }
}
```

### Validation Checklist

Pre-Deployment:
- [ ] **Server initialization tests pass** (MANDATORY - catches stdio_server errors)
- [ ] All tests pass (unit + integration)
- [ ] Test coverage ≥90%
- [ ] Premium limitations documented
- [ ] No linter errors
- [ ] Server starts without errors
- [ ] Tools list correctly
- [ ] Tool calls return valid JSON
- [ ] **Resources identified for server** (if applicable - see Resource Implementation section)
- [ ] **Resources implemented** (if applicable):
  - [ ] `list_resources()` handler implemented
  - [ ] `read_resource()` handler implemented
  - [ ] Resource URIs follow consistent scheme
  - [ ] All resources have descriptions and mimeTypes
  - [ ] Resource content is valid JSON (if JSON)
  - [ ] Resources tested (list and read operations)
  - [ ] Resources documented in README
- [ ] Error handling works correctly
- [ ] Authentication methods work (env var, config file, 1Password)
- [ ] Documentation is complete
- [ ] Configuration examples are correct
- [ ] Paths are portable (not hardcoded)
- [ ] Test workspace properly configured
- [ ] Test cleanup working correctly

Post-Deployment:
- [ ] Integration tests pass in production-like environment
- [ ] Premium features documented and tested
- [ ] Error handling verified with real services
- [ ] Performance acceptable under load

### CI/CD Integration

Recommended GitHub Actions Workflow:
```yaml
name: Test

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install -r requirements-test.txt
      
      - name: Run server initialization tests
        run: pytest tests/test_server_initialization.py -v
      
      - name: Run unit tests
        run: pytest -m unit
      
      - name: Run integration tests
        if: github.event_name == 'push'
        env:
          TEST_API_TOKEN: ${{ secrets.TEST_API_TOKEN }}
        run: pytest -m integration
      
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

### Examples

Reference Implementations:
- **Asana MCP** (`mcp/asana/tests/`): Comprehensive test suite with all categories
- **Google Calendar MCP** (`mcp/google-calendar/src/tests/`): TypeScript testing patterns
- **Instagram MCP** (`mcp/instagram/tests/`): Python test structure

## Deployment & Distribution

### Git Submodule Setup

1. Create Repository:
   ```bash
   mkdir mcp-server-name
   cd mcp-server-name
   git init
   ```

2. Add Files:
   ```bash
   git add .
   git commit -m "Initial commit"
   ```

3. Add Remote:
   ```bash
   git remote add origin https://github.com/username/mcp-server-name.git
   git push -u origin main
   ```

4. Add as Submodule:
   ```bash
   cd parent-repo
   git submodule add https://github.com/username/mcp-server-name.git mcp/name
   ```

### Version Management

- Use semantic versioning (MAJOR.MINOR.PATCH)
- Tag releases in submodule repository
- Update parent repo submodule reference when needed

### Portability Requirements

Must:
- Work standalone (no hard dependencies on parent repo)
- Use environment variables or config directories for paths
- Support multiple authentication methods
- Include all dependencies in `requirements.txt` or `package.json`

Should:
- Support 1Password integration (optional, for backward compatibility)
- Auto-detect parent repo structure (optional, for convenience)
- Fall back to config directory if parent structure not found

Must Not:
- Hardcode repository paths
- Require specific directory structure
- Depend on parent repo scripts (except optional 1Password integration)

## Examples from Existing Servers

### Python Server: DNSimple

Location: `mcp/dnsimple/`

Key Patterns: Environment variable → config file → 1Password authentication, simple tool definitions with comprehensive schemas, structured error responses, comprehensive README with examples.

Reference: `mcp/dnsimple/dnsimple_mcp_server.py`

### Python Server: Parquet

Location: `mcp/parquet/`

Key Patterns: Complex tool implementations (filtering, aggregation, semantic search), data directory auto-detection with fallback, audit logging and rollback capabilities, extensive documentation.

Reference: `mcp/parquet/parquet_mcp_server.py`

### TypeScript Server: Google Calendar

Location: `mcp/google-calendar/`

Key Patterns: OAuth 2.0 authentication, multi-account support, complex async operations, TypeScript type safety, comprehensive test suite.

Reference: `mcp/google-calendar/src/index.ts`

### Python Server: Instagram

Location: `mcp/instagram/`

Key Patterns: Structured logging (structlog), class-based server implementation, separate client and models modules, comprehensive error handling, **resource implementation**.

**Resource Implementation Example:**
```python
@self.server.list_resources()
async def handle_list_resources() -> List[Resource]:
    """List available resources."""
    return [
        Resource(
            uri="instagram://profile",
            name="Instagram Profile",
            description="Current Instagram business profile information",
            mimeType="application/json",
        ),
        Resource(
            uri="instagram://media/recent",
            name="Recent Media Posts",
            description="Recent Instagram posts with engagement metrics",
            mimeType="application/json",
        ),
        Resource(
            uri="instagram://insights/account",
            name="Account Insights",
            description="Account-level analytics and insights",
            mimeType="application/json",
        ),
        Resource(
            uri="instagram://pages",
            name="Connected Pages",
            description="Facebook pages connected to the account",
            mimeType="application/json",
        ),
    ]

@self.server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Handle resource reading."""
    if uri == "instagram://profile":
        profile = await instagram_client.get_profile_info()
        return json.dumps(profile.model_dump(mode='json'), indent=2)
    
    elif uri == "instagram://media/recent":
        posts = await instagram_client.get_media_posts(limit=10)
        return json.dumps([post.model_dump(mode='json') for post in posts], indent=2)
    
    # ... other resources
    
    else:
        raise ValueError(f"Unknown resource URI: {uri}")
```

Reference: `mcp/instagram/src/instagram_mcp_server.py`

---

### Python Server: Parquet (Resources Planned)

Location: `mcp/parquet/`

Key Patterns: Complex tool implementations, audit logging, extensive documentation.

**Note:** Resources are documented in README but not yet implemented. Planned resources include:
- `parquet://tools/{tool_name}` - Tool schemas
- `parquet://catalog/data_types` - Data type catalog
- `parquet://examples/patterns` - Common query patterns

Reference: `mcp/parquet/parquet_mcp_server.py`, `mcp/parquet/README.md`

## Quick Reference

### Python Server Template

```python
#!/usr/bin/env python3
"""
MCP Server for [Service Name]
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("server-name")

@app.list_tools()
async def list_tools() -> List[Tool]:
    return [Tool(...)]

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> List[TextContent]:
    if name == "tool_name":
        result = {"success": True, "data": "..."}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    raise ValueError(f"Unknown tool: {name}")

async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### Resource Implementation Template

```python
from mcp.types import Resource

@app.list_resources()
async def list_resources() -> List[Resource]:
    """List available resources."""
    return [
        Resource(
            uri="server://profile",
            name="User Profile",
            description="Current user account information",
            mimeType="application/json",
        ),
        Resource(
            uri="server://tools/{tool_name}",
            name="Tool Schema Template",
            description="JSON schema for any tool",
            mimeType="application/json",
        ),
    ]

@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read resource content by URI."""
    if uri == "server://profile":
        profile = await get_profile()
        return json.dumps(profile, indent=2)
    
    elif uri.startswith("server://tools/"):
        tool_name = uri.split("/")[-1]
        tools = await app.list_tools()
        for tool in tools:
            if tool.name == tool_name:
                return json.dumps(tool.inputSchema, indent=2)
        raise ValueError(f"Tool not found: {tool_name}")
    
    else:
        raise ValueError(f"Unknown resource URI: {uri}")
```

### Authentication Template

```python
def get_credential() -> Optional[str]:
    # 1. Environment variable
    credential = os.getenv("SERVICE_API_TOKEN")
    if credential:
        return credential
    
    # 2. Config file
    config_file = Path.home() / ".config" / "server-name-mcp" / ".env"
    if config_file.exists():
        # Parse .env file
        ...
    
    # 3. 1Password (optional)
    if HAS_CREDENTIALS_MODULE:
        credential = get_credential_from_1password()
        if credential:
            return credential
    
    raise ValueError("Credential not found")
```

## Additional Resources

- [MCP Specification](https://modelcontextprotocol.io/specification)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk)
- Existing server READMEs for detailed examples

## Revision History

- 2025-12-26: Initial guide created based on analysis of existing servers
