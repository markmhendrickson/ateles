# Setup Guide

## 1. Clone the Repository

```bash
git clone <repository-url>
cd personal  # or your repo directory name
```

## 2. Check Python Version

**Important:** The MCP server requires Python 3.10 or higher.

```bash
# Check your Python version
python3 --version

# If you have Python 3.9 or lower, install a newer version:
# macOS (via Homebrew):
brew install python@3.11  # or python@3.12

# Verify newer Python is available
python3.11 --version  # or python3.12
```

## 3. Set Up Python Environment

```bash
# Create virtual environment with Python 3.10+ (recommended)
# If you installed python3.11 via Homebrew:
python3.11 -m venv venv

# Or use system Python if it's 3.10+:
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate  # On macOS/Linux
# or: venv\Scripts\activate  # On Windows

# Upgrade pip
pip install --upgrade pip

# Install dependencies
pip install -r execution/scripts/requirements.txt
```

## 4. Configure MCP Servers

MCP servers are located in `mcp/` and pre-configured in `.cursor/mcp.json` for Cursor IDE. To use them:

```bash
# Install Parquet MCP server dependencies (requires Python 3.10+)
cd mcp/parquet
pip install -r requirements.txt
cd ../..

# Install Minted MCP server dependencies
cd mcp/minted
pip install -r requirements.txt
cd ../..

# Install Gmail MCP server dependencies (requires Node.js)
cd mcp/gmail
npm install
npm run build
cd ../..
```

**Important:** The `.cursor/mcp.json` file uses `${REPO_ROOT}` for path resolution. If you see `ModuleNotFoundError` for `pyarrow` or `mcp`, verify:

1. The venv Python has the dependencies:
   ```bash
   source venv/bin/activate
   python -c "import pyarrow, pandas, mcp; print('✓ All dependencies available')"
   ```

2. The MCP config uses the venv Python (not system Python):
   ```bash
   # Check .cursor/mcp.json - should use ${REPO_ROOT} or absolute paths
   cat .cursor/mcp.json
   ```

### For Cursor IDE

- MCP servers are automatically configured via `.cursor/mcp.json`
- Five servers available: `parquet`, `gmail`, `minted`, `instagram`, `google-calendar`
- Restart Cursor after installing dependencies
- See `mcp/README.md` for complete configuration

### Troubleshooting MCP Servers

**If you see `ModuleNotFoundError: No module named 'pyarrow'`:**
- The MCP config is using system Python instead of venv Python
- Update `.cursor/mcp.json` to use: `"${REPO_ROOT}/venv/bin/python3"`

**If you see `ModuleNotFoundError: No module named 'mcp'`:**
- Python version is too old (need 3.10+)
- Upgrade Python and recreate venv (see step 2)

### For Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp/parquet/parquet_mcp_server.py"]
    },
    "gmail": {
      "command": "node",
      "args": ["/absolute/path/to/mcp/gmail/dist/index.js"]
    },
    "minted": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp/minted/minted_mcp_server.py"]
    }
  }
}
```

## 5. Set Up 1Password CLI

**Required for credential management and environment variable sync.**

```bash
# Run setup script (installs if needed, verifies authentication)
./execution/scripts/setup-1password-cli.sh

# If not signed in, sign in:
eval $(op signin)
```

**Note:** The setup script will:
- Check if 1Password CLI is installed
- Install it via Homebrew on macOS if missing
- Verify authentication status
- Provide instructions if manual setup is needed

See `/docs/credential_management.md` for detailed 1Password CLI setup instructions.

## 6. Configure Environment Variables

### Option 1: Sync from 1Password (Recommended)

```bash
# Ensure 1Password CLI is signed in (if not already)
eval $(op signin)

# Sync credentials to .env
python execution/scripts/op_sync_env_from_1password.py
```

### Option 2: Manual Configuration

Create `.env` in the repo root:

```env
COINBASE_API_KEY="your_key_here"
COINBASE_API_SECRET="your_secret_here"
COINBASE_API_PASSPHRASE="your_passphrase_here"
HIRO_PLATFORM_API_KEY="your_hiro_key_here"
```

**Note:** `.env` is already in `.gitignore` - never commit it.

## 7. Verify Setup

```bash
# Activate virtual environment first
source venv/bin/activate

# Test MCP server (should wait for stdio input - this is normal)
python mcp/parquet/parquet_mcp_server.py
# Press Ctrl+C to exit

# Test data query
python execution/scripts/query_transactions.py --summary

# Verify MCP dependencies
python -c "import mcp, pyarrow, pandas; print('✓ MCP server dependencies ready')"

# List available data types via MCP (in Cursor, ask: "List all available data types")
```

## 8. Optional: Set Up Background Services

### Asana Sync Service

```bash
./execution/scripts/setup-asana-sync.sh
```

### Audio Transcription Watcher

```bash
./execution/scripts/setup-audio-transcription-watcher.sh
```

### Twilio SMS Services

```bash
./execution/scripts/setup-twilio-sms-services.sh
```

### Asana Webhook Services

```bash
./execution/scripts/setup-asana-webhook-services.sh
```

See individual setup scripts for detailed configuration instructions.

