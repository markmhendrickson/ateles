# Automatic MCP Configuration Generation

The MCP configuration (`.cursor/mcp.json`) is **automatically generated** from the template (`mcp/mcp-config-template.json`) in these scenarios:

## Automatic Triggers

### 1. Repository Initialization
- **When**: Running `./setup.sh` (first-time setup)
- **Action**: Generates `.cursor/mcp.json` from template
- **Hook**: `setup.sh` calls `generate_mcp_config.py`

### 2. Git Clone/Checkout
- **When**: Cloning repo or checking out branches
- **Action**: Initializes submodules + regenerates MCP config
- **Hook**: `post-checkout`

### 3. Pull/Merge Updates
- **When**: Pulling changes or merging branches
- **Action**: Initializes new submodules + regenerates config if template changed
- **Hook**: `post-merge`

### 4. Template Changes
- **When**: Committing changes to `mcp/mcp-config-template.json`
- **Action**: Regenerates MCP config after commit
- **Hook**: `post-commit`

### 5. Rebase/Amend
- **When**: Running `git rebase` or `git commit --amend`
- **Action**: Regenerates MCP config
- **Hook**: `post-rewrite`

## Manual Generation

If you need to manually regenerate the config:

```bash
./scripts/generate_mcp_config.py
```

## Adding a New MCP Server

1. **Add submodule** (if applicable):
   ```bash
   git submodule add <repo-url> mcp/<server-name>
   ```

2. **Update template**: Edit `mcp/mcp-config-template.json` to add the new server

3. **Commit changes**: The `post-commit` hook will automatically regenerate the config

4. **Or manually generate**: Run `./scripts/generate_mcp_config.py`

## Benefits

- ✅ **No manual steps** - Config stays in sync automatically
- ✅ **Template is source of truth** - Single place to manage servers
- ✅ **Works on clone** - New users get correct config automatically
- ✅ **Catches changes** - Template updates trigger regeneration
