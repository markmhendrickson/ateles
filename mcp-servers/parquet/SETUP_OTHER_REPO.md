# Setting Up Parquet MCP Server in Another Repository

This guide explains how to include the parquet MCP server in a different Cursor workspace.

## Quick Start: Reference This Repo's Server

### Option A: Using REPO_ROOT Variable

To reference this repo's parquet server using a relative path and inherit `DATA_DIR` from your environment:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "${REPO_ROOT}/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

**Requirements:**
- `DATA_DIR` must be set in your environment (shell or `.env` file)
- Dependencies installed: `pip install -r requirements.txt` in the parquet directory
- `${REPO_ROOT}` resolves to this repository's root path

### Option B: Using Relative Path from Sibling Repository

If your other repository is a sibling to the `personal` repo (e.g., both in `~/repos/`):

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "../personal/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {
        "DATA_DIR": "${env:DATA_DIR}"
      }
    }
  }
}
```

**Requirements:**
- Relative path `../personal/` must resolve correctly from your workspace root
- `${env:DATA_DIR}` references the `DATA_DIR` environment variable
- Dependencies installed: `pip install -r requirements.txt` in the parquet directory

**Note:** The relative path is resolved from the workspace root where the MCP configuration is located.

## Option 1: Copy Server to New Repository (Recommended)

### Step 1: Copy the Server Files

Copy the entire `parquet` directory to your new repository:

```bash
# From your new repository root
cp -r /path/to/personal/mcp/parquet ./mcp-servers/parquet
# Or wherever you want to organize it in your new repo
```

**Required files:**
- `parquet_mcp_server.py` (main server file)
- `requirements.txt` (dependencies)
- `__init__.py` (if present)

**Optional files (documentation):**
- `README.md`
- `SETUP.md`
- `AUDIT_LOG_GUIDE.md`
- etc.

### Step 2: Install Dependencies

```bash
cd mcp-servers/parquet
pip install -r requirements.txt
```

**Dependencies:**
- `mcp>=0.9.0` (MCP SDK)
- `pandas>=2.0.0`
- `pyarrow>=12.0.0`
- `numpy>=1.24.0`
- `openai>=1.0.0` (optional, for semantic search)

### Step 3: Set Up Data Directory

Create your data directory structure:

```bash
# In your new repository
mkdir -p data
# The server will create subdirectories automatically as needed
```

### Step 4: Configure Cursor MCP Settings

Add to your Cursor MCP configuration file (`~/.cursor/mcp.json` or workspace-specific settings):

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "/absolute/path/to/your/new/repo/mcp-servers/parquet/parquet_mcp_server.py"
      ],
      "env": {
        "DATA_DIR": "/absolute/path/to/your/new/repo/data"
      }
    }
  }
}
```

**Important:** Use absolute paths for the server script and `DATA_DIR`.

### Step 5: Restart Cursor

Restart Cursor to load the new MCP server configuration.

---

## Option 2: Reference Server from Original Repository (Shared Setup) ⭐ RECOMMENDED

If you want to use the same server code from your personal repository in multiple workspaces:

### Step 1: Ensure Dependencies are Installed

Make sure dependencies are installed in the Python environment that will run the server:

```bash
cd /Users/markmhendrickson/repos/ateles/mcp/parquet
pip install -r requirements.txt
```

### Step 2: Configure Cursor MCP Settings

Add to your Cursor MCP configuration (`~/.cursor/mcp.json` or workspace-specific settings):

**Option A: Use REPO_ROOT variable and inherit DATA_DIR** ⭐ RECOMMENDED

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "${REPO_ROOT}/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

**Key Points:**
- Use `${REPO_ROOT}` relative path to reference this repo's parquet server
- Leave `env` empty `{}` to inherit `DATA_DIR` from your current environment
- The server will use whatever `DATA_DIR` is set in your shell/environment
- Works seamlessly if you have `DATA_DIR` set globally or in your `.env` file

**Option A2: Use relative path from sibling repo with explicit env variable**

If your repos are siblings (e.g., both in `~/repos/`):

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "../personal/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {
        "DATA_DIR": "${env:DATA_DIR}"
      }
    }
  }
}
```

**Key Points:**
- Use relative path `../personal/` from your workspace root
- `${env:DATA_DIR}` explicitly references the environment variable
- Path is resolved relative to where the MCP config file is located

**Option B: Use relative path with explicit DATA_DIR**

If you want to use a different `DATA_DIR` for this workspace:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "${REPO_ROOT}/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {
        "DATA_DIR": "/absolute/path/to/your/data/directory"
      }
    }
  }
}
```

**Note:** `${REPO_ROOT}` is a Cursor variable that resolves to the workspace root. If it doesn't work, you can use an absolute path or relative path from the workspace.

### Step 3: Ensure DATA_DIR is Set

Make sure `DATA_DIR` is set in your environment:

```bash
# Check if DATA_DIR is set
echo $DATA_DIR

# If not set, add to your shell profile (~/.zshrc, ~/.bashrc, etc.)
export DATA_DIR="/path/to/your/data/directory"

# Or set it in your .env file in the repo root
echo 'DATA_DIR="/path/to/your/data/directory"' >> .env
```

The server will automatically read `DATA_DIR` from:
1. Environment variable (highest priority)
2. `.env` file in the repo root (if using dotenv)

### Step 4: Restart Cursor

Restart Cursor to load the configuration.

### Benefits of This Approach

✅ **Single source of truth** - One server codebase to maintain  
✅ **Relative paths** - Use `${REPO_ROOT}` for portability  
✅ **Environment inheritance** - Use existing `DATA_DIR` configuration  
✅ **Easy updates** - Update server code once, all repos benefit  
✅ **No duplication** - Don't need to copy server files to each repo

---

## Option 3: Use Environment Variables (Workspace-Specific)

You can also configure the data directory per workspace using environment variables:

### Step 1: Create Workspace-Specific Configuration

In your new repository, create a `.env` file (if the server supports it) or use Cursor's environment variable settings:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "/path/to/parquet_mcp_server.py"
      ],
      "env": {
        "DATA_DIR": "${workspaceFolder}/data"
      }
    }
  }
}
```

**Note:** Cursor may support `${workspaceFolder}` variable, but absolute paths are more reliable.

---

## Verification

After setup, verify the server is working:

1. **Check MCP Server Status**: In Cursor, check if the `parquet` MCP server appears in your available MCP servers
2. **Test a Query**: Try using an MCP tool like `list_data_types` to see if it connects
3. **Check Logs**: If there are issues, check Cursor's MCP server logs

---

## Data Directory Structure

The server expects data in this structure:

```
data/
├── [data_type]/
│   └── [data_type].parquet
├── schemas/
│   └── [data_type]_schema.json
├── logs/
│   └── audit_log.parquet
└── snapshots/
    └── [data_type]-[timestamp].parquet
```

The server will create these directories automatically when you first use it.

---

## Troubleshooting

### Server Not Appearing

1. **Check Python Path**: Ensure `python3` is in your PATH and points to the correct Python installation
2. **Check File Permissions**: Ensure the server script is executable or can be run with `python3`
3. **Check Dependencies**: Run `pip install -r requirements.txt` in the server directory
4. **Check Paths**: Verify all paths in the MCP configuration are absolute and correct

### Data Directory Issues

1. **Check DATA_DIR**: Verify the `DATA_DIR` environment variable points to the correct location
2. **Check Permissions**: Ensure the data directory is writable
3. **Check Structure**: The server will create the structure automatically, but you can create it manually if needed

### Import Errors

If you see import errors:
1. Install dependencies: `pip install -r requirements.txt`
2. Check Python version (requires Python 3.8+)
3. Verify virtual environment is activated if using one

---

## Example: Complete Setup for New Repo

```bash
# 1. In your new repository
mkdir -p mcp-servers/parquet
cd mcp-servers/parquet

# 2. Copy files (adjust source path)
cp /path/to/personal/mcp/parquet/parquet_mcp_server.py .
cp /path/to/personal/mcp/parquet/requirements.txt .
touch __init__.py  # If needed

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create data directory
cd ../..
mkdir -p data

# 5. Add to ~/.cursor/mcp.json (or workspace settings)
# Use absolute paths!
```

---

## Notes

- **Absolute Paths**: Always use absolute paths in MCP configuration for reliability
- **Data Isolation**: Each repository can have its own `DATA_DIR` for data isolation
- **Shared Server**: You can use the same server code with different data directories
- **Version Control**: Consider whether to commit the server code to your new repository or reference it externally
