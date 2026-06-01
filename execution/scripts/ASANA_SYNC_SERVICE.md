# Asana Sync Service

The Asana sync script can run in two modes:

1. **One-time sync** - Run manually when needed
2. **Background service** - Runs continuously, syncing at regular intervals

## Quick Start

### One-Time Sync

```bash
# Run a single sync cycle
python scripts/sync_asana_tasks.py

# Preview changes without applying
python scripts/sync_asana_tasks.py --dry-run
```

### Background Service (macOS)

Install as a LaunchAgent to run automatically on system startup:

```bash
# Install and start the service
./scripts/setup-asana-sync.sh
```

This will:
- Install the LaunchAgent to `~/Library/LaunchAgents/`
- Start the service immediately
- Configure it to start automatically on login
- Set up log files at `data/logs/asana_sync.log`

**Management Commands:**

```bash
# Check if service is running
launchctl list | grep asana-sync

# Stop the service (without uninstalling)
launchctl unload ~/Library/LaunchAgents/com.finances.asana-sync.plist

# Start the service manually
launchctl load ~/Library/LaunchAgents/com.finances.asana-sync.plist

# View logs
tail -f data/logs/asana_sync.log
tail -f data/logs/asana_sync.error.log

# Uninstall (remove from startup)
launchctl unload ~/Library/LaunchAgents/com.finances.asana-sync.plist
rm ~/Library/LaunchAgents/com.finances.asana-sync.plist
```

### Manual Background Mode

Run the sync script directly in daemon mode:

```bash
# Run with default 60-second interval
python scripts/sync_asana_tasks.py --daemon

# Custom interval (300 seconds = 5 minutes)
python scripts/sync_asana_tasks.py --daemon --interval 300

# Stop with Ctrl+C
```

### Using Screen/Tmux (Alternative)

For manual background execution without LaunchAgent:

```bash
# Using screen
screen -S asana-sync
python scripts/sync_asana_tasks.py --daemon --interval 60
# Press Ctrl+A, D to detach

# Reattach later
screen -r asana-sync

# Using tmux
tmux new -s asana-sync
python scripts/sync_asana_tasks.py --daemon --interval 60
# Press Ctrl+B, D to detach

# Reattach later
tmux attach -t asana-sync
```

## Configuration

### Sync Interval

Default: 60 seconds

The sync interval determines how often the script polls both Asana workspaces for changes. Shorter intervals provide more real-time sync but use more API calls.

**Recommended intervals:**
- **60 seconds** - Real-time sync (default)
- **300 seconds (5 min)** - Balanced
- **600 seconds (10 min)** - Lower API usage

### Environment Variables

Required (set in `.env` file):
- `ASANA_SOURCE_PAT` - Personal Access Token for source workspace
- `ASANA_TARGET_PAT` - Personal Access Token for target workspace (optional, defaults to source PAT)
- `SOURCE_WORKSPACE_GID` - Source workspace GID
- `TARGET_WORKSPACE_GID` - Target workspace GID

Optional:
- `FALLBACK_ASSIGNEE_EMAIL` - Email for task assignment
- `ALLOW_OVERWRITE` - Allow overwriting existing tasks

## How It Works

The sync service ensures all tasks exist in three locations:
1. **Local parquet** (`$DATA_DIR/tasks/tasks.parquet`)
2. **Source Asana workspace**
3. **Target Asana workspace**

**Sync Flow:**
1. Source → Local: Fetch tasks from source workspace
2. Target → Local: Fetch tasks from target workspace
3. Cross-workspace: Create tasks in missing workspaces
4. Local → Source: Update/create tasks in source workspace
5. Local → Target: Update/create tasks in target workspace

**Efficiency:**
- Only fetches tasks modified since last sync (`modified_at` comparison)
- Skips unchanged tasks automatically
- Creates snapshots before modifications
- Uses incremental sync to minimize API calls

## Troubleshooting

### Service Not Starting

```bash
# Check LaunchAgent status
launchctl list | grep asana-sync

# Check logs for errors
tail -f data/logs/asana_sync.error.log

# Verify environment variables are set
cat .env | grep ASANA
```

### API Rate Limits

If you hit Asana API rate limits, increase the sync interval:

```bash
# Edit the wrapper script to use longer interval
# Or run manually with custom interval
python scripts/sync_asana_tasks.py --daemon --interval 300
```

### Tasks Not Syncing

1. Check sync state file: `data/logs/asana_sync_state.json`
2. Verify workspace GIDs are correct
3. Check that PATs have proper permissions
4. Review error logs: `data/logs/asana_sync.error.log`

## Alternatives

### Cron (Linux/macOS)

For scheduled syncs instead of continuous polling:

```bash
# Add to crontab (runs every 5 minutes)
*/5 * * * * cd /path/to/finances && python scripts/sync_asana_tasks.py
```

### Systemd (Linux)

Create a systemd service file for Linux systems (similar to LaunchAgent on macOS).

### Webhooks (Future)

Asana supports webhooks, but they require:
- Public HTTP endpoint
- SSL certificate
- Webhook management infrastructure

Polling is simpler for local/private deployments.








