# HomeKit mDNS Auto-Discovery with Docker on macOS

## The Problem

HomeKit uses mDNS (multicast DNS) for automatic device discovery. When Home Assistant runs in Docker on macOS, mDNS advertisements from inside the container don't propagate to your Mac's network because:

1. **Docker Desktop uses a Linux VM** - Containers run in a VM, not directly on macOS
2. **Bridge networking isolates mDNS** - Multicast traffic from containers doesn't reach the host network
3. **macOS firewall** - May block mDNS even if it reaches the host

## Solutions (Ranked by Reliability)

### ✅ Solution 1: Manual IP Pairing (Recommended - Always Works)

**Pros:**
- Works immediately
- Bypasses all mDNS issues
- Most reliable method

**Steps:**
1. Get pairing code from Home Assistant notifications
2. In Home app: "+" → "I Don't Have a Code" → "Add Manually"
3. Enter: IP `192.168.0.252`, Port `21064`, Code (from HA)

### ⚠️ Solution 2: Run Home Assistant Natively (Not in Docker)

**Pros:**
- mDNS works properly
- Direct network access

**Cons:**
- More complex setup
- Requires Python environment management

**Steps:**
```bash
# Use the existing venv
cd /Users/markmhendrickson/repos/personal
source execution/homeassistant-venv/bin/activate
hass
```

### ⚠️ Solution 3: mDNS Reflector (May Work)

**Pros:**
- Keeps Docker setup
- Can bridge mDNS traffic

**Cons:**
- May not work perfectly with Docker bridge networking
- Requires additional software

**Steps:**
```bash
./scripts/setup_mdns_reflector.sh
```

### ❌ Solution 4: Docker Host Networking (Doesn't Work on macOS)

Docker Desktop on macOS doesn't support `--network=host` mode. This only works on Linux.

## Current Status

- ✅ Port 21064 is accessible (manual pairing works)
- ✅ Home Assistant is running
- ✅ HomeKit integration is configured
- ❌ mDNS auto-discovery not working (Docker limitation)

## Recommendation

**Use manual IP pairing** - it's the most reliable method and works immediately. Auto-discovery is nice-to-have but not essential for functionality.
