# MCP Configuration Generation

## Overview

The MCP configuration uses a **template-based approach**:

1. **`mcp/mcp-config-template.json`** - Source of truth (template with placeholders)
2. **`.cursor/mcp.json`** - Generated config (absolute paths, used by Cursor)

## Why Two Files?

- **Template** (`mcp/mcp-config-template.json`):
  - Version-controlled (can be committed)
  - Contains placeholders (`/absolute/path/to/repo`)
  - Documents all available MCP servers
  - Source of truth for configuration structure

- **Actual Config** (`.cursor/mcp.json`):
  - User-specific (absolute paths)
  - Generated from template
  - Used by Cursor IDE
  - Should not be committed (contains absolute paths)

## Generating Config

To generate `.cursor/mcp.json` from the template:

```bash
./scripts/generate_mcp_config.py
```

This script:
1. Reads `mcp/mcp-config-template.json`
2. Replaces `/absolute/path/to/repo` with actual repo path
3. Verifies wrapper scripts exist
4. Generates `.cursor/mcp.json` with absolute paths

## Workflow

1. **Add new MCP server**: Update `mcp/mcp-config-template.json`
2. **Generate config**: Run `./scripts/generate_mcp_config.py`
3. **Restart Cursor**: Load the updated configuration

## Benefits

- ✅ Template is version-controlled (shows all available servers)
- ✅ Actual config is user-specific (absolute paths)
- ✅ Single source of truth (template)
- ✅ Easy to add new servers (just update template)
- ✅ No manual path editing needed
