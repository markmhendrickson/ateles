# Home Assistant Launchd Service Management

The Home Assistant service is configured to run automatically in the background using macOS launchd.

## Service Details

- **Service Name**: `com.homeassistant.server`
- **Plist File**: `~/Library/LaunchAgents/com.homeassistant.server.plist` (copy from `execution/scripts/com.homeassistant.server.plist`)
- **Logs**:
  - Standard output: `data/logs/homeassistant.log` (in repository)
  - Errors: `data/logs/homeassistant.error.log` (in repository)
- **Config Directory**: `~/.homeassistant`

## Installation

To set up the Home Assistant service:

```bash
# Run the setup script from the repository
cd /Users/markmhendrickson/repos/ateles
./execution/scripts/setup-homeassistant-service.sh
```

This will:
- Create the logs directory (`data/logs/`)
- Install the LaunchAgent plist file
- Start the service automatically

## Service Management Commands

### Check Service Status

```bash
# Check if service is loaded
launchctl list | grep homeassistant

# Check service status
launchctl list com.homeassistant.server
```

### Start Service

```bash
launchctl load ~/Library/LaunchAgents/com.homeassistant.server.plist
```

### Stop Service

```bash
launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
```

### Restart Service

```bash
launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
launchctl load ~/Library/LaunchAgents/com.homeassistant.server.plist
```

### View Logs

```bash
# View standard output
tail -f data/logs/homeassistant.log

# View errors
tail -f data/logs/homeassistant.error.log

# View both
tail -f data/logs/homeassistant.log data/logs/homeassistant.error.log
```

### Check if Home Assistant is Running

```bash
# Check if web interface is accessible
curl http://localhost:8123/api/

# Or check process
ps aux | grep hass
```

## Automatic Behavior

- **Starts automatically**: When you log in to your Mac
- **Restarts automatically**: If Home Assistant crashes (KeepAlive is enabled)
- **Runs in background**: No terminal window needed

## Troubleshooting

### Service Won't Start

1. **Check logs**:
   ```bash
   cat data/logs/homeassistant.error.log
   ```

2. **Verify venv path**:
   ```bash
   ls -la execution/homeassistant-venv/bin/hass
   ```

3. **Test manual start**:
   ```bash
   cd /Users/markmhendrickson/repos/ateles
   source execution/homeassistant-venv/bin/activate
   hass
   ```

### Service Keeps Crashing

1. **Check error logs** for specific issues
2. **Verify Python version**: Should be 3.11+
3. **Check disk space**: Home Assistant needs space for database
4. **Check port availability**: Port 8123 should be free

### Update Home Assistant

1. **Stop service**:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
   ```

2. **Update in venv**:
   ```bash
   cd /Users/markmhendrickson/repos/ateles
   source execution/homeassistant-venv/bin/activate
   pip install --upgrade homeassistant
   ```

3. **Start service**:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.homeassistant.server.plist
   ```

## Disable Auto-Start

To prevent Home Assistant from starting automatically:

```bash
launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
```

To re-enable:

```bash
launchctl load ~/Library/LaunchAgents/com.homeassistant.server.plist
```

## Manual Start (Without Service)

If you want to run Home Assistant manually instead:

```bash
cd /Users/markmhendrickson/repos/ateles
source execution/homeassistant-venv/bin/activate
hass
```

This will run in the foreground and show all output in the terminal.
