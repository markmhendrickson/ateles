# Home Assistant Troubleshooting

## Current Issue: 500 Internal Server Error

**Status:** Home Assistant 2024.3.3 has dependency conflicts with Python 3.11.14, specifically with the `acme`/`josepy` packages used by the `hass-nabucasa` (cloud) component.

**Error:** `AttributeError: module 'josepy' has no attribute 'ComparableX509'`

## Solutions

### Option 1: Use Docker (Recommended)

Docker provides a clean, isolated environment with all dependencies properly configured:

```bash
# Install Docker Desktop for Mac
# Then run:
docker run -d \
  --name homeassistant \
  --privileged \
  --restart=unless-stopped \
  -e TZ=Europe/Madrid \
  -v ~/.homeassistant:/config \
  --network=host \
  ghcr.io/home-assistant/home-assistant:stable
```

**Benefits:**
- No dependency conflicts
- Automatic updates
- Isolated environment
- Easy to remove/reinstall

### Option 2: Upgrade Python to 3.12

Home Assistant 2024.3.3 warns that Python 3.11 is deprecated. Upgrade to Python 3.12:

```bash
# Install Python 3.12 via Homebrew
brew install python@3.12

# Create new venv with Python 3.12
python3.12 -m venv ~/homeassistant-venv-py312
source ~/homeassistant-venv-py312/bin/activate
pip install --upgrade pip setuptools wheel
pip install homeassistant

# Update launchd service to use new venv
# Edit ~/Library/LaunchAgents/com.homeassistant.server.plist
# Change path to: /Users/markmhendrickson/homeassistant-venv-py312/bin/hass
```

### Option 3: Upgrade Home Assistant to Latest

The current installation is stuck at 2024.3.3. Try forcing upgrade:

```bash
cd /Users/markmhendrickson/repos/ateles
source execution/homeassistant-venv/bin/activate
pip install --upgrade --force-reinstall homeassistant
```

**Note:** This may still have dependency conflicts. Docker is the most reliable solution.

### Option 4: Disable Cloud Component Entirely

If you don't need cloud features, try removing the component:

```bash
# Remove hass-nabucasa
cd /Users/markmhendrickson/repos/ateles
source execution/homeassistant-venv/bin/activate
pip uninstall -y hass-nabucasa

# Edit configuration.yaml to explicitly disable cloud
# Add to configuration.yaml:
default_config:
  cloud: !disable
```

**Note:** This may cause other components to fail if they depend on cloud features.

## Current Configuration

- **Home Assistant Version:** 2024.3.3
- **Python Version:** 3.11.14 (deprecated)
- **Installation Type:** Python venv (in repository at `execution/homeassistant-venv`)
- **Service:** launchd (com.homeassistant.server)
- **Config Location:** ~/.homeassistant/

## Next Steps

1. **Recommended:** Switch to Docker installation (Option 1)
2. **Alternative:** Upgrade to Python 3.12 and reinstall (Option 2)
3. **Quick Fix:** Try disabling cloud component (Option 4)

## Service Management

```bash
# Check status
launchctl list | grep homeassistant

# View logs
tail -f data/logs/homeassistant.log
tail -f data/logs/homeassistant.error.log

# Restart service
launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
launchctl load ~/Library/LaunchAgents/com.homeassistant.server.plist

# Stop service
launchctl unload ~/Library/LaunchAgents/com.homeassistant.server.plist
pkill -f hass
```
