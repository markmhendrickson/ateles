# Home Assistant Installation Guide for macOS

This guide covers installing Home Assistant on macOS. There are two main approaches:

## Option 1: Docker Installation (Recommended - Easiest)

### Step 1: Install Docker Desktop

1. **Download Docker Desktop for Mac**:
   - Visit: https://www.docker.com/products/docker-desktop/
   - Download Docker Desktop for Mac (Apple Silicon or Intel, depending on your Mac)
   - Open the downloaded `.dmg` file
   - Drag Docker to Applications folder
   - Launch Docker from Applications
   - Follow the setup wizard

2. **Verify Docker is running**:
   ```bash
   docker --version
   docker ps
   ```

### Step 2: Install Home Assistant Container

1. **Create a directory for Home Assistant**:
   ```bash
   mkdir -p ~/homeassistant
   cd ~/homeassistant
   ```

2. **Run Home Assistant in Docker**:
   ```bash
   docker run -d \
     --name homeassistant \
     --privileged \
     --restart=unless-stopped \
     -e TZ=Europe/Madrid \
     -v ~/homeassistant:/config \
     --network=host \
     ghcr.io/home-assistant/home-assistant:stable
   ```

   **Note**: On macOS, `--network=host` doesn't work. Use port mapping instead:
   ```bash
   docker run -d \
     --name homeassistant \
     --privileged \
     --restart=unless-stopped \
     -e TZ=Europe/Madrid \
     -v ~/homeassistant:/config \
     -p 8123:8123 \
     ghcr.io/home-assistant/home-assistant:stable
   ```

3. **Access Home Assistant**:
   - Open browser: http://localhost:8123
   - Wait 2-5 minutes for initial setup
   - Follow the setup wizard to create your account

4. **Check status**:
   ```bash
   docker ps
   docker logs homeassistant
   ```

### Step 3: Access from Other Devices

To access Home Assistant from other devices on your network:

1. **Find your Mac's IP address**:
   ```bash
   ifconfig | grep "inet " | grep -v 127.0.0.1
   ```
   Or check System Settings → Network

2. **Access from other devices**:
   - Use: `http://[your-mac-ip]:8123`
   - Example: `http://192.168.1.100:8123`

## Option 2: Python venv Installation (No Docker Required)

### Step 1: Install Python Dependencies

```bash
# Install Python 3.11+ if needed
brew install python@3.11

# Create virtual environment in repository
cd /Users/markmhendrickson/repos/ateles
python3.11 -m venv execution/homeassistant-venv
source execution/homeassistant-venv/bin/activate

# Install required system dependencies
brew install sqlite3
```

### Step 2: Install Home Assistant Core

```bash
# Upgrade pip
pip install --upgrade pip

# Install Home Assistant
pip install homeassistant

# Create config directory
mkdir -p ~/.homeassistant
```

### Step 3: Run Home Assistant

```bash
# Activate venv (if not already active)
cd /Users/markmhendrickson/repos/ateles
source execution/homeassistant-venv/bin/activate

# Run Home Assistant
hass
```

### Step 4: Access Home Assistant

- Open browser: http://localhost:8123
- Wait for initial setup
- Follow setup wizard

### Step 5: Run as Background Service (Optional)

Create a launchd service to run Home Assistant automatically:

1. **Run the setup script** (recommended):
   ```bash
   cd /Users/markmhendrickson/repos/ateles
   ./execution/scripts/setup-homeassistant-service.sh
   ```

   This automatically:
   - Creates the logs directory
   - Installs the LaunchAgent plist file
   - Starts the service

2. **Or manually create plist file**:
   ```bash
   # Copy the plist from the repository
   cp execution/scripts/com.homeassistant.server.plist ~/Library/LaunchAgents/com.homeassistant.server.plist
   
   # Update paths in the plist (replace /Users/markmhendrickson/repos/ateles with actual repo path)
   # Then load the service:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.homeassistant.server.plist
   ```

   **Note:** Logs are stored in `data/logs/homeassistant.log` and `data/logs/homeassistant.error.log` within the repository.

## Option 3: Home Assistant OS (Raspberry Pi - Recommended for Production)

For a dedicated device, consider installing Home Assistant OS on a Raspberry Pi:

1. **Requirements**:
   - Raspberry Pi 4 or 5 (2GB+ RAM)
   - 32GB+ microSD card
   - Ethernet cable

2. **Installation**:
   - Download Home Assistant OS image
   - Flash to SD card using Raspberry Pi Imager
   - Insert SD card, connect Ethernet, power on
   - Access at `http://homeassistant.local:8123`

## Post-Installation: Configure for HomeKit MCP Server

After Home Assistant is running:

1. **Create Long-Lived Access Token**:
   - Open Home Assistant web interface
   - Click your profile (bottom left)
   - Scroll to "Long-Lived Access Tokens"
   - Click "Create Token"
   - Name: "HomeKit MCP Server"
   - Copy the token

2. **Configure HomeKit MCP Server**:
   - Follow instructions in `CONFIGURATION.md`
   - Use URL: `http://localhost:8123/api` (if on same Mac)
   - Or: `http://[your-mac-ip]:8123/api` (from other devices)

3. **Add Netatmo Integration**:
   - In Home Assistant: Settings → Devices & Services
   - Click "Add Integration"
   - Search for "Netatmo"
   - Follow setup wizard
   - Your Legrand/Netatmo devices should appear

## Troubleshooting

### Docker Issues

- **Port already in use**: Change port mapping: `-p 8124:8123`
- **Permission errors**: Check Docker Desktop is running
- **Container won't start**: Check logs: `docker logs homeassistant`

### Python venv Issues

- **Import errors**: Make sure venv is activated
- **Port conflicts**: Change port in `configuration.yaml`: `http: port: 8124`
- **Performance**: Docker is generally faster on macOS

### Network Access

- **Can't access from other devices**:
  - Check firewall settings
  - Verify Mac's IP address
  - Ensure Home Assistant is bound to `0.0.0.0` (not just `127.0.0.1`)

## Recommended Setup

For best results:
- **Development/Testing**: Docker on Mac (Option 1)
- **Production**: Home Assistant OS on Raspberry Pi (Option 3)
- **Lightweight**: Python venv (Option 2)

## Migration Between Installation Methods

**Yes, you can migrate between installation methods!** Home Assistant stores all configuration and data in a config directory that can be moved.

### Migration Path: Python venv → Docker

1. **Stop Home Assistant** (if running):
   ```bash
   # If running as service
   launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist

   # Or if running manually, just stop it (Ctrl+C)
   ```

2. **Locate your config directory**:
   - Default location: `~/.homeassistant`
   - Or check `configuration.yaml` for custom path

3. **Install Docker** (if not already installed):
   ```bash
   brew install --cask docker
   ```

4. **Run Home Assistant in Docker with existing config**:
   ```bash
   docker run -d \
     --name homeassistant \
     --privileged \
     --restart=unless-stopped \
     -e TZ=Europe/Madrid \
     -v ~/.homeassistant:/config \
     -p 8123:8123 \
     ghcr.io/home-assistant/home-assistant:stable
   ```

5. **Verify migration**:
   - Access: http://localhost:8123
   - All your devices, automations, and settings should be intact

### Migration Path: Python venv → Raspberry Pi

1. **Stop Home Assistant on Mac**:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
   ```

2. **Copy config directory to Raspberry Pi**:
   ```bash
   # From your Mac
   scp -r ~/.homeassistant pi@raspberry-pi-ip:/home/pi/.homeassistant
   ```

3. **On Raspberry Pi**, ensure Home Assistant OS is installed

4. **Copy config to Home Assistant OS**:
   - Home Assistant OS stores config in `/config`
   - Use Samba share or SSH to copy files
   - Or use Home Assistant backup/restore feature

5. **Restart Home Assistant on Raspberry Pi**

### Migration Path: Docker → Raspberry Pi

1. **Stop Docker container**:
   ```bash
   docker stop homeassistant
   ```

2. **Copy config directory**:
   ```bash
   # If config is in ~/homeassistant
   scp -r ~/homeassistant pi@raspberry-pi-ip:/home/pi/
   ```

3. **On Raspberry Pi**, copy to `/config` or use backup/restore

### Best Practice: Use Backup/Restore Feature

Home Assistant has a built-in backup feature that makes migration easier:

1. **Create backup** (in Home Assistant):
   - Settings → System → Backups
   - Click "Create Backup"
   - Download the backup file

2. **On new installation**:
   - Complete initial setup wizard
   - Settings → System → Backups
   - Click "Upload Backup"
   - Select your backup file
   - Restore

This method works for any migration path and is the safest approach.

### What Gets Migrated

✅ **Migrated** (in config directory):
- All device configurations
- Automations and scripts
- Integrations and their settings
- Custom components
- History database
- User accounts and preferences
- Themes and UI customizations

❌ **Not migrated** (need to reconfigure):
- System-specific paths (if any)
- Network bindings (may need adjustment)
- Port numbers (if changed)

## Next Steps

After installation:
1. ✅ Complete Home Assistant setup wizard
2. ✅ Add Netatmo integration
3. ✅ Create long-lived access token
4. ✅ Configure HomeKit MCP server (see `CONFIGURATION.md`)
5. ✅ Test device control via MCP server
