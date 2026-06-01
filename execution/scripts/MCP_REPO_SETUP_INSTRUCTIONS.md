# MCP Server Repository Setup Instructions

## Current Status

All MCP server repositories are committed locally and ready to push:
- `mcp-servers/gmail` - 2 commits ahead
- `mcp-servers/google-calendar` - 2 commits ahead  
- `mcp-servers/instagram` - 1 commit ahead
- `mcp-servers/parquet` - Ready to push
- `mcp-servers/minted` - Ready to push

## Option 1: Using GitHub CLI (Recommended)

1. **Install GitHub CLI:**
   ```bash
   brew install gh
   gh auth login
   ```

2. **Run the setup script:**
   ```bash
   ./scripts/create_and_push_mcp_repos.sh
   ```

This will:
- Create all 5 GitHub repositories
- Push all commits to origin

## Option 2: Manual Creation via Web

1. **Create repositories on GitHub:**
   - Go to https://github.com/new
   - Create each repository:
     - `mcp-server-gmail` (public)
     - `mcp-server-google-calendar` (public)
     - `mcp-server-instagram` (public)
     - `mcp-server-parquet` (public)
     - `mcp-server-minted` (public)

2. **Push commits:**
   ```bash
   cd mcp-servers/gmail && git push -u origin main
   cd ../google-calendar && git push -u origin main
   cd ../instagram && git push -u origin main
   cd ../parquet && git push -u origin main
   cd ../minted && git push -u origin main
   ```

## Option 3: Use Existing Script

After creating repos manually:
```bash
./scripts/push_nested_repos.sh
```

## Repository URLs

Once created, repositories will be at:
- https://github.com/markmhendrickson/mcp-server-gmail
- https://github.com/markmhendrickson/mcp-server-google-calendar
- https://github.com/markmhendrickson/mcp-server-instagram
- https://github.com/markmhendrickson/mcp-server-parquet
- https://github.com/markmhendrickson/mcp-server-minted

