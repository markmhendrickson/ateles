# Create MCP Server Forks with Custom Names

## Option A: Install GitHub CLI and Create Forks

1. Install GitHub CLI:
   ```bash
   brew install gh
   gh auth login
   ```

2. Create forks with custom names:
   ```bash
   gh repo fork GongRzhe/Gmail-MCP-Server --clone=false --repo markmhendrickson/mcp-server-gmail
   gh repo fork nspady/google-calendar-mcp --clone=false --repo markmhendrickson/mcp-server-google-calendar
   gh repo fork jlbadano/ig-mcp --clone=false --repo markmhendrickson/mcp-server-instagram
   ```

## Option B: Manual Fork Creation with Custom Names

Since GitHub's web interface doesn't allow renaming during fork, you'll need to:

1. **Fork the repositories** (they'll have original names):
   - Fork https://github.com/GongRzhe/Gmail-MCP-Server
   - Fork https://github.com/nspady/google-calendar-mcp
   - Fork https://github.com/jlbadano/ig-mcp

2. **Rename each fork** after creation:
   - Go to each repository's Settings → General → Repository name
   - Rename to:
     - `mcp-server-gmail`
     - `mcp-server-google-calendar`
     - `mcp-server-instagram`

## Option C: Create New Repositories (Not Forks)

If you want complete independence (not linked as forks):

1. Create new repositories on GitHub:
   - `markmhendrickson/mcp-server-gmail`
   - `markmhendrickson/mcp-server-google-calendar`
   - `markmhendrickson/mcp-server-instagram`

2. Push your local repos:
   ```bash
   ./scripts/push_nested_repos.sh
   ```

## After Creating Forks/Repositories

Run the push script:
```bash
./scripts/push_nested_repos.sh
```

Or manually:
```bash
cd mcp-servers/gmail && git push -u origin main
cd ../google-calendar && git push -u origin main
cd ../instagram && git push -u origin main
```

## Organizing in GitHub

To organize these in GitHub:

1. **Create a GitHub Organization** (optional):
   - Create organization: `markmhendrickson-mcps` or similar
   - Transfer repositories to the organization

2. **Use Topics** (recommended):
   - Add topic `mcp-server` to all three repositories
   - Add specific topics: `gmail`, `google-calendar`, `instagram`

3. **Use a README** in a parent directory:
   - Create a repository like `markmhendrickson/mcp-servers` with links to all MCP servers

