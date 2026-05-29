# Automatic Submodule Initialization

This repository automatically initializes git submodules using git hooks. Submodules are initialized automatically when:

1. **Cloning the repository** - The `post-checkout` hook runs after the initial clone
2. **Checking out branches** - The `post-checkout` hook runs on branch switches
3. **Pulling/merging updates** - The `post-merge` hook runs after pulls/merges

## How It Works

### Git Hooks

The following hooks are installed in `.git/hooks/`:

- **`post-checkout`** - Runs after checkout/clone, initializes submodules if needed
- **`post-merge`** - Runs after merge/pull, initializes submodules if needed

### Installation

Hooks are automatically installed by:

1. **`setup.sh`** - Bootstrap script for new clones
2. **`scripts/install_git_hooks.sh`** - Manual installation script
3. **`post-checkout` hook itself** - Self-installs on first checkout (for new clones)

### Manual Initialization

If you need to manually initialize submodules:

```bash
# All submodules
./scripts/init_submodules.sh

# Or using git directly
git submodule update --init --recursive

# Check status
./scripts/ensure_submodules.sh
```

## MCP Server Submodules

The following MCP servers are managed as submodules:

- `mcp/gmail` - Gmail integration
- `mcp/google-calendar` - Google Calendar integration
- `mcp/instagram` - Instagram Business API
- `mcp/dnsimple` - DNSimple domain management
- `mcp/minted` - Minted.com API
- `mcp/parquet` - Parquet data access (Truth Layer)
- `mcp/asana` - Asana project management

## Troubleshooting

### Hooks Not Running

If submodules aren't initializing automatically:

1. **Check if hooks are installed:**
   ```bash
   ls -la .git/hooks/post-checkout .git/hooks/post-merge
   ```

2. **Reinstall hooks:**
   ```bash
   ./scripts/install_git_hooks.sh
   ```

3. **Manually initialize:**
   ```bash
   ./scripts/init_submodules.sh
   ```

### Submodule Status

Check which submodules are initialized:

```bash
git submodule status
```

The output shows:
- `-` prefix = Not initialized
- ` ` (space) = Initialized
- `+` prefix = Has uncommitted changes

### Cloning Without Submodules

If you clone without `--recurse-submodules`, the hooks will initialize them automatically on first checkout.

## Configuration

Submodules are configured in `.gitmodules`. To add a new submodule:

```bash
git submodule add <repository-url> <path>
```

The hook will automatically initialize it on next checkout/merge.
